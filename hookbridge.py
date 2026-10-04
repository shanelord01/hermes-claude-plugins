"""Run Claude Code plugin hooks from Hermes lifecycle hooks.

Claude event        Hermes hook            What carries over
SessionStart        pre_llm_call (first)   additionalContext or plain stdout goes into the turn's context
UserPromptSubmit    pre_llm_call           additionalContext or plain stdout goes into the turn's context;
                                           a block cannot stop the prompt, so its reason is passed on instead
Stop                on_session_end         Hermes cannot keep a finished turn going, so a block's reason is
                                           delivered with the next turn
PreToolUse          pre_tool_call          deny (or exit code 2) blocks the tool call with the hook's reason
PostToolUse         post_tool_call         run for its side effects; output is logged
SessionEnd          on_session_finalize    run for its side effects

Hooks run only for plugins whose hooks the user accepted (`hermes claude-plugins consent`), and only
for the exact hooks they accepted: an update that changes a plugin's hooks withdraws consent.
Commands run through /bin/sh with Claude Code's stdin JSON and environment (CLAUDE_PLUGIN_ROOT,
CLAUDE_PROJECT_DIR). Hooks that read `transcript_path` get a Claude-format JSONL transcript
written from the Hermes conversation.
"""
import json
import logging
import os
import re
import subprocess
import threading

from . import paths, store, translate

logger = logging.getLogger("hermes_claude_plugins.hooks")

HOOK_TIMEOUT_CAP = 25  # Hermes bounds hook callbacks at 30 s and fails pre_tool_call closed after that
TOOL_TO_CLAUDE = {
    "terminal": "Bash", "read_file": "Read", "write_file": "Write", "patch": "Edit",
    "search_files": "Grep", "web_search": "WebSearch", "web_extract": "WebFetch", "todo": "TodoWrite",
}
CONTEXT_LIMIT = 20000


class Bridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []
        self._state_mtime = None
        self._seen_sessions = set()
        self._pending = {}          # session_id -> [messages to deliver next turn]
        self._stop_active = set()   # sessions whose last Stop hook asked to continue

    # ---- registry -------------------------------------------------------
    def _reload(self):
        try:
            mtime = os.path.getmtime(paths.state_file())
        except OSError:
            mtime = None
        if mtime == self._state_mtime:
            return
        self._state_mtime = mtime
        entries = []
        for key, rec in store.load()["plugins"].items():
            if not rec.get("hooks_consent") or rec.get("disabled"):
                continue
            hooks = rec.get("hooks") or {}
            if translate.hooks_digest(hooks) != rec.get("consented_digest"):
                continue
            for event, matcher, command, timeout in translate.hook_commands(hooks):
                if event in translate.SUPPORTED_EVENTS:
                    entries.append({"plugin": rec["name"], "key": key, "root": rec["package"], "event": event,
                                    "matcher": matcher or "", "command": command, "timeout": timeout})
        self._entries = entries
        logger.debug("claude-plugins hook bridge: %d hooks active", len(entries))

    def entries(self, event):
        with self._lock:
            self._reload()
            return [e for e in self._entries if e["event"] == event]

    # ---- running one hook -------------------------------------------------
    @staticmethod
    def _matches(matcher, value):
        if matcher in ("", "*"):
            return True
        try:
            return re.fullmatch(matcher, value or "") is not None
        except re.error:
            return matcher == value

    @staticmethod
    def _cwd():
        cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()
        return cwd if os.path.isdir(cwd) else os.path.expanduser("~")

    def run(self, entry, payload):
        """Return (exit code, parsed JSON or None, stdout text, stderr text)."""
        cwd = payload.get("cwd") or self._cwd()
        env = dict(os.environ)
        env.update({"CLAUDE_PLUGIN_ROOT": entry["root"], "CLAUDE_PROJECT_DIR": cwd,
                    "HERMES_CLAUDE_PLUGINS": "1", "HERMES_SESSION_ID": str(payload.get("session_id") or "")})
        timeout = min(float(entry.get("timeout") or HOOK_TIMEOUT_CAP), HOOK_TIMEOUT_CAP)
        try:
            proc = subprocess.run(["/bin/sh", "-c", entry["command"]], input=json.dumps(payload), capture_output=True,
                                  text=True, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("claude-plugins: %s %s hook timed out after %ss", entry["plugin"], entry["event"], timeout)
            return None, None, "", "timed out"
        except OSError as exc:
            logger.warning("claude-plugins: %s %s hook could not start: %s", entry["plugin"], entry["event"], exc)
            return None, None, "", str(exc)
        out = (proc.stdout or "").strip()
        parsed = None
        if out.startswith("{"):
            try:
                parsed = json.loads(out)
            except ValueError:
                parsed = None
        if proc.returncode not in (0, 2):
            logger.warning("claude-plugins: %s %s hook exited %s: %s", entry["plugin"], entry["event"],
                           proc.returncode, (proc.stderr or "").strip()[:300])
        return proc.returncode, parsed, out, (proc.stderr or "").strip()

    @staticmethod
    def _context(code, parsed, out):
        if code != 0:
            return ""
        if isinstance(parsed, dict):
            spec = parsed.get("hookSpecificOutput") or {}
            return str(spec.get("additionalContext") or parsed.get("additionalContext") or "")
        return out

    def _base(self, event, session_id, cwd=None):
        return {"session_id": session_id or "", "transcript_path": self._transcript_path(session_id),
                "cwd": cwd or self._cwd(), "hook_event_name": event}

    # ---- transcript -------------------------------------------------------
    @staticmethod
    def _transcript_path(session_id):
        if not session_id:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))[:120]
        return str(paths.transcripts_dir() / ("%s.jsonl" % safe))

    def write_transcript(self, session_id, history, user_message):
        path = self._transcript_path(session_id)
        if not path:
            return
        try:
            paths.ensure(paths.transcripts_dir(), private=True)
            lines = []
            for msg in list(history or []) + ([{"role": "user", "content": user_message}] if user_message else []):
                role = msg.get("role") if isinstance(msg, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if role == "user":
                    lines.append({"type": "user", "message": {"role": "user", "content": content if isinstance(content, (str, list)) else str(content)}})
                elif role == "assistant":
                    text = content if isinstance(content, str) else json.dumps(content) if content else ""
                    lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
                elif role == "tool":
                    lines.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": str(content)[:2000]}]}})
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(json.dumps(line) + "\n")
        except Exception as exc:
            logger.debug("claude-plugins: transcript write failed: %s", exc)

    # ---- Hermes hook callbacks --------------------------------------------
    def pre_llm_call(self, session_id=None, user_message=None, conversation_history=None, is_first_turn=False, **_):
        session_id = session_id or ""
        starting = self.entries("SessionStart")
        prompting = self.entries("UserPromptSubmit")
        pending = self._pending.pop(session_id, [])
        if not (starting or prompting or pending):
            return None
        self.write_transcript(session_id, conversation_history, user_message)
        parts = list(pending)
        if starting and (is_first_turn or session_id not in self._seen_sessions):
            self._seen_sessions.add(session_id)
            for entry in starting:
                payload = dict(self._base("SessionStart", session_id), source="startup")
                if not self._matches(entry["matcher"], "startup"):
                    continue
                code, parsed, out, _err = self.run(entry, payload)
                text = self._context(code, parsed, out)
                if text:
                    parts.append(text)
        for entry in prompting:
            payload = dict(self._base("UserPromptSubmit", session_id), prompt=user_message or "")
            code, parsed, out, err = self.run(entry, payload)
            if code == 2 or (isinstance(parsed, dict) and parsed.get("decision") == "block"):
                reason = err if code == 2 else parsed.get("reason", "")
                parts.append("A UserPromptSubmit hook from the Claude Code plugin %s asked to block this prompt "
                             "(Hermes cannot block it): %s" % (entry["plugin"], reason))
                continue
            text = self._context(code, parsed, out)
            if text:
                parts.append(text)
        context = "\n\n".join(p for p in parts if p).strip()
        if not context:
            return None
        return {"context": context[:CONTEXT_LIMIT]}

    def on_session_end(self, session_id=None, completed=True, interrupted=False, **_):
        entries = self.entries("Stop")
        if not entries or interrupted:
            return None
        session_id = session_id or ""
        active = session_id in self._stop_active
        self._stop_active.discard(session_id)
        for entry in entries:
            payload = dict(self._base("Stop", session_id), stop_hook_active=active)
            code, parsed, out, err = self.run(entry, payload)
            reason = ""
            if code == 2:
                reason = err
            elif isinstance(parsed, dict) and parsed.get("decision") == "block":
                reason = str(parsed.get("reason") or "")
            if reason:
                self._pending.setdefault(session_id, []).append(
                    "The Claude Code plugin %s's Stop hook asked for this before you finish: %s" % (entry["plugin"], reason))
                self._stop_active.add(session_id)
        return None

    def pre_tool_call(self, tool_name=None, args=None, session_id=None, tool_call_id=None, **_):
        entries = self.entries("PreToolUse")
        if not entries:
            return None
        claude_name = TOOL_TO_CLAUDE.get(tool_name or "", tool_name or "")
        for entry in entries:
            if not (self._matches(entry["matcher"], claude_name) or self._matches(entry["matcher"], tool_name or "")):
                continue
            payload = dict(self._base("PreToolUse", session_id), tool_name=claude_name, tool_input=args or {},
                           tool_use_id=tool_call_id or "", hermes_tool_name=tool_name or "")
            code, parsed, _out, err = self.run(entry, payload)
            if code is None:
                return {"action": "block", "message": "Claude Code plugin %s's PreToolUse hook did not answer in time." % entry["plugin"]}
            reason = None
            if code == 2:
                reason = err or "blocked"
            elif isinstance(parsed, dict):
                spec = parsed.get("hookSpecificOutput") or {}
                decision = (spec.get("permissionDecision") or parsed.get("decision") or "").lower()
                if decision in ("deny", "block", "ask"):
                    reason = spec.get("permissionDecisionReason") or parsed.get("reason") or decision
            if reason:
                return {"action": "block", "message": "Blocked by the Claude Code plugin %s: %s" % (entry["plugin"], reason)}
        return None

    def post_tool_call(self, tool_name=None, args=None, result=None, session_id=None, tool_call_id=None, **_):
        entries = self.entries("PostToolUse")
        claude_name = TOOL_TO_CLAUDE.get(tool_name or "", tool_name or "")
        for entry in entries:
            if self._matches(entry["matcher"], claude_name) or self._matches(entry["matcher"], tool_name or ""):
                payload = dict(self._base("PostToolUse", session_id), tool_name=claude_name, tool_input=args or {},
                               tool_response=result if isinstance(result, (dict, list, str)) else str(result),
                               tool_use_id=tool_call_id or "")
                self.run(entry, payload)
        return None

    def on_session_finalize(self, session_id=None, **_):
        for entry in self.entries("SessionEnd"):
            self.run(entry, dict(self._base("SessionEnd", session_id), reason="other"))
        self._seen_sessions.discard(session_id or "")
        self._pending.pop(session_id or "", None)
        path = self._transcript_path(session_id)
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        return None
