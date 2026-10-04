"""Tests for hermes-claude-plugins. No network and no Hermes install needed:
    python3 -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="hcp-test-"))
os.environ["HERMES_HOME"] = str(TMP / "hermes")


def _load_package():
    spec = importlib.util.spec_from_file_location("hcp", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["hcp"] = module
    spec.loader.exec_module(module)
    return module


hcp = _load_package()
from hcp import cli, fetch, hookbridge, ops, paths, store, translate  # noqa: E402

HOOK_SCRIPT = textwrap.dedent("""\
    import json, os, sys
    data = json.load(sys.stdin)
    event = data["hook_event_name"]
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if event == "SessionStart":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
              "additionalContext": "START from %s source=%s" % (os.path.basename(root), data.get("source"))}}))
    elif event == "UserPromptSubmit":
        print("PROMPT saw: " + data["prompt"])
    elif event == "Stop":
        if not data.get("stop_hook_active"):
            print(json.dumps({"decision": "block", "reason": "save your work first"}))
    elif event == "PreToolUse":
        if "rm -rf" in json.dumps(data.get("tool_input")):
            print("no recursive deletes", file=sys.stderr)
            sys.exit(2)
    elif event == "SessionEnd":
        open(os.path.join(os.environ["MARKER_DIR"], "ended"), "w").write(data["session_id"])
    """)


def make_marketplace(base: Path, hooks_extra=None) -> Path:
    mkt = base / "mkt"
    plug = mkt / "plugins" / "demo"
    (mkt / ".claude-plugin").mkdir(parents=True)
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "testmkt", "owner": {"name": "t"},
        "plugins": [{"name": "demo", "source": "./plugins/demo", "description": "demo plugin"}]}))
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "demo", "version": "1.2.3", "description": "A demo", "author": {"name": "Tester"}, "license": "MIT"}))
    (plug / "skills" / "greet").mkdir(parents=True)
    (plug / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Say hello\n---\nRun ${CLAUDE_PLUGIN_ROOT}/bin/greet\n")
    (plug / "commands" / "sub").mkdir(parents=True)
    (plug / "commands" / "hello.md").write_text(
        "---\ndescription: Greet someone\nargument-hint: \"<name>\"\n---\n\nSay hello to $ARGUMENTS using ${CLAUDE_PLUGIN_ROOT}/bin/greet.\n")
    (plug / "commands" / "sub" / "deep.md").write_text("Deep command body.\n")
    (plug / "agents").mkdir()
    (plug / "agents" / "helper.md").write_text("---\nname: helper\n---\nA subagent.\n")
    (plug / "hooks").mkdir()
    (plug / "hooks" / "hook.py").write_text(HOOK_SCRIPT)
    cmd = "%s \"${CLAUDE_PLUGIN_ROOT}/hooks/hook.py\"" % sys.executable
    hooks = {
        "SessionStart": [{"matcher": "startup|resume", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}],
        "Stop": [{"hooks": [{"type": "command", "command": cmd}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": cmd}]}],
        "PreCompact": [{"hooks": [{"type": "command", "command": cmd}]}],
    }
    hooks.update(hooks_extra or {})
    (plug / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    (plug / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "local": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/server", "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"]},
        "remote": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-Test": "1"}},
        "legacy": {"type": "sse", "url": "https://mcp.example.com/sse"},
        "absolute": {"command": "/usr/local/bin/thing"},
        "envy": {"command": "npx", "args": ["${API_TOKEN}"]},
    }}))
    return mkt


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(paths.hermes_home(), ignore_errors=True)
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.mkt = make_marketplace(self.work)
        os.environ["MARKER_DIR"] = str(self.work)
        ops.marketplace_add(str(self.mkt))


class TestSources(unittest.TestCase):
    def test_parse_source(self):
        self.assertEqual(fetch.parse_source("owner/repo")["source"], "github")
        self.assertEqual(fetch.parse_source("owner/repo#v2")["ref"], "v2")
        self.assertEqual(fetch.parse_source("https://github.com/o/r.git")["repo"], "o/r")
        self.assertEqual(fetch.parse_source("https://gitlab.com/o/r.git")["source"], "git")
        self.assertEqual(fetch.parse_source("https://x.example/marketplace.json")["source"], "json-url")
        self.assertEqual(fetch.parse_source(str(TMP))["source"], "local")
        with self.assertRaises(fetch.FetchError):
            fetch.parse_source("not a source")

    def test_tarball_escape_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(fetch.FetchError):
            fetch._safe_extract(buf.getvalue(), TMP / "extract")


class TestInstall(Base):
    def test_marketplace_list(self):
        self.assertIn("testmkt", "\n".join(ops.marketplace_list()))

    def test_install_translates(self):
        report = "\n".join(ops.install("demo"))
        pkg = paths.package_dir("demo")
        manifest = json.loads((pkg / "plugin.json").read_text())
        self.assertEqual(manifest["$schema"], translate.AGENT_PLUGIN_SCHEMA)
        self.assertEqual(manifest["name"], "claude-demo")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertIn(str(pkg), (pkg / "skills" / "greet" / "SKILL.md").read_text())
        mcp = json.loads((pkg / "mcp.json").read_text())
        self.assertEqual(mcp["$schema"], translate.AGENT_MCP_SCHEMA)
        self.assertEqual(mcp["mcpServers"]["local"], {"type": "stdio", "command": "./bin/server", "args": ["--root", "${PLUGIN_ROOT}"]})
        self.assertEqual(mcp["mcpServers"]["remote"]["type"], "streamable-http")
        self.assertNotIn("legacy", mcp["mcpServers"])
        self.assertNotIn("absolute", mcp["mcpServers"])
        self.assertNotIn("envy", mcp["mcpServers"])
        for text in ("SSE transport", "bare executable", "environment placeholders", "agents/", "PreCompact hooks"):
            self.assertIn(text, report)
        self.assertIn("NOT RUN until you accept them", report)
        hello = (paths.command_skills_dir() / "demo-hello" / "SKILL.md").read_text()
        self.assertIn("name: demo-hello", hello)
        self.assertIn('description: "Greet someone"', hello)
        self.assertIn("$ARGUMENTS", hello)
        self.assertIn("(expected: <name>)", hello)
        self.assertIn(str(pkg) + "/bin/greet", hello)
        self.assertTrue((paths.command_skills_dir() / "demo-sub-deep" / "SKILL.md").is_file())

    def test_hooks_need_consent(self):
        ops.install("demo")
        bridge = hookbridge.Bridge()
        self.assertEqual(bridge.entries("SessionStart"), [])
        ops.consent("demo@testmkt")
        self.assertEqual(len(bridge.entries("SessionStart")), 1)
        ops.consent("demo", revoke=True)
        self.assertEqual(bridge.entries("SessionStart"), [])

    def test_changed_hooks_withdraw_consent(self):
        ops.install("demo", accept_hooks=True)
        self.assertTrue(store.load()["plugins"]["demo@testmkt"]["hooks_consent"])
        ops.sync()
        self.assertTrue(store.load()["plugins"]["demo@testmkt"]["hooks_consent"], "unchanged hooks keep consent")
        shutil.rmtree(self.mkt)
        make_marketplace(self.work, {"PostToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]})
        ops.update("demo")
        self.assertFalse(store.load()["plugins"]["demo@testmkt"]["hooks_consent"])

    def test_uninstall(self):
        ops.install("demo")
        ops.uninstall("demo")
        self.assertFalse(paths.package_dir("demo").exists())
        self.assertFalse((paths.command_skills_dir() / "demo-hello").exists())
        self.assertEqual(store.load()["plugins"], {})
        self.assertIn("removed", ops.marketplace_remove("testmkt"))

    def test_remove_marketplace_in_use(self):
        ops.install("demo")
        with self.assertRaises(ops.OpError):
            ops.marketplace_remove("testmkt")

    def test_slash_command(self):
        ops.install("demo")
        self.assertIn("demo@testmkt 1.2.3", cli.handle_slash("list"))
        self.assertIn("hooks (not accepted)", cli.handle_slash("show demo"))
        self.assertIn("hermes claude-plugins install", cli.handle_slash("bogus"))
        self.assertIn("is not installed", cli.handle_slash("show nope"))


class TestHookBridge(Base):
    def setUp(self):
        super().setUp()
        ops.install("demo", accept_hooks=True)
        self.bridge = hookbridge.Bridge()

    def test_session_start_and_prompt(self):
        first = self.bridge.pre_llm_call(session_id="s1", user_message="hi there", conversation_history=[], is_first_turn=True)
        self.assertIn("START from claude-demo source=startup", first["context"])
        self.assertIn("PROMPT saw: hi there", first["context"])
        second = self.bridge.pre_llm_call(session_id="s1", user_message="again", conversation_history=[{"role": "user", "content": "hi there"}])
        self.assertNotIn("START", second["context"])
        lines = Path(self.bridge._transcript_path("s1")).read_text().splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines], ["user", "user"])

    def test_stop_reason_delivered_next_turn(self):
        self.bridge.on_session_end(session_id="s2", completed=True)
        nxt = self.bridge.pre_llm_call(session_id="s2", user_message="ok", conversation_history=[])
        self.assertIn("save your work first", nxt["context"])
        self.bridge.on_session_end(session_id="s2", completed=True)  # stop_hook_active now true
        again = self.bridge.pre_llm_call(session_id="s2", user_message="ok", conversation_history=[])
        self.assertNotIn("save your work first", again["context"])

    def test_pre_tool_call(self):
        blocked = self.bridge.pre_tool_call(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, session_id="s3")
        self.assertEqual(blocked["action"], "block")
        self.assertIn("no recursive deletes", blocked["message"])
        self.assertIsNone(self.bridge.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s3"))
        self.assertIsNone(self.bridge.pre_tool_call(tool_name="read_file", args={"path": "rm -rf"}, session_id="s3"))

    def test_session_end(self):
        self.bridge.pre_llm_call(session_id="s4", user_message="x", conversation_history=[], is_first_turn=True)
        self.bridge.on_session_finalize(session_id="s4")
        self.assertEqual((self.work / "ended").read_text(), "s4")
        self.assertFalse(Path(self.bridge._transcript_path("s4")).exists())


class TestRegister(unittest.TestCase):
    def test_register(self):
        seen = {"cli": [], "cmd": [], "hooks": []}

        class Ctx:
            def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
                seen["cli"].append(name)

            def register_command(self, name, handler, description="", args_hint=""):
                seen["cmd"].append(name)

            def register_hook(self, name, cb):
                seen["hooks"].append(name)

        hcp.register(Ctx())
        self.assertEqual(seen["cli"], ["claude-plugins"])
        self.assertEqual(seen["cmd"], ["claude-plugins"])
        self.assertEqual(sorted(seen["hooks"]), sorted(["pre_llm_call", "on_session_end", "pre_tool_call",
                                                        "post_tool_call", "on_session_finalize"]))


SHAREDBRAIN = Path(os.path.expanduser("~/projects/claude-mempalace-sharedbrain"))


@unittest.skipUnless((SHAREDBRAIN / ".claude-plugin" / "marketplace.json").is_file(), "sharedbrain checkout not present")
class TestRealPlugin(unittest.TestCase):
    """The mempalace-sharedbrain plugin through the bridge: commands, hooks, and its session-start output."""

    def test_sharedbrain(self):
        shutil.rmtree(paths.hermes_home(), ignore_errors=True)
        ops.marketplace_add(str(SHAREDBRAIN))
        report = "\n".join(ops.install("mempalace-sharedbrain", accept_hooks=True))
        self.assertIn("/mempalace-sharedbrain-inbox", report)
        if "modules" in json.loads((SHAREDBRAIN / "hooks" / "hooks.json").read_text()):
            self.assertIn("a Claude Code mod", report)
        self.assertIn("PreCompact hooks", report)
        cfg = TMP / "sb-config.json"
        cfg.write_text(json.dumps({"identity": {"host": "testhost", "harness": "hermes"}, "hub": {"transport": "none"}}))
        os.environ["MEMPALACE_SHAREDBRAIN_CONFIG"] = str(cfg)
        os.environ["MEMPALACE_SHAREDBRAIN_STATE"] = str(TMP / "sb-state")
        try:
            out = hookbridge.Bridge().pre_llm_call(session_id="sb1", user_message="hello", conversation_history=[], is_first_turn=True)
        finally:
            os.environ.pop("MEMPALACE_SHAREDBRAIN_CONFIG")
            os.environ.pop("MEMPALACE_SHAREDBRAIN_STATE")
        self.assertIn("MEMPALACE SHARED BRAIN", out["context"])
        self.assertIn("testhost:hermes:", out["context"])


if __name__ == "__main__":
    unittest.main()
