"""Read a Claude Code plugin and translate it for Hermes.

What maps where:
- skills/<name>/SKILL.md      -> skills of an Agent Plugins v1 package that Hermes loads natively
- .mcp.json / mcpServers      -> that package's mcp.json (stdio and streamable-http; no SSE)
- commands/*.md               -> one generated skill per command, so it becomes a /slash command
- hooks/hooks.json / hooks    -> run at runtime by hookbridge.py, only with consent
- agents/, output styles, LSP -> not expressible in Hermes; reported, not installed
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from . import paths

AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PORTABLE_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
ROOT_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
SUPPORTED_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse", "SessionEnd")


class TranslateError(Exception):
    pass


# ---- reading a Claude plugin --------------------------------------------

def _read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest(plugin_dir: Path, entry: dict = None) -> dict:
    """.claude-plugin/plugin.json, with the marketplace entry filling gaps (non-strict marketplaces)."""
    manifest = {}
    path = plugin_dir / ".claude-plugin" / "plugin.json"
    if path.is_file():
        manifest = _read_json(path)
    for key, value in (entry or {}).items():
        if key in ("source", "strict", "category", "tags"):
            continue
        manifest.setdefault(key, value)
    if not manifest.get("name"):
        raise TranslateError("%s has no plugin name (.claude-plugin/plugin.json or a marketplace entry)" % plugin_dir)
    return manifest


def _inside(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if base.resolve() != target and base.resolve() not in target.parents:
        raise TranslateError("path %s escapes the plugin" % rel)
    return target


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def command_files(plugin_dir: Path, manifest: dict) -> list:
    """(command name, path) pairs. commands/sub/x.md is named sub:x, as Claude Code does."""
    found = []
    roots = [plugin_dir / "commands"] + [_inside(plugin_dir, p) for p in _as_list(manifest.get("commands"))]
    seen = set()
    for root in roots:
        if root.is_file() and root.suffix == ".md":
            items = [(root.stem, root)]
        elif root.is_dir():
            items = [(":".join(p.relative_to(root).with_suffix("").parts), p) for p in sorted(root.rglob("*.md"))]
        else:
            items = []
        for name, path in items:
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            found.append((name, path))
    return found


def skill_dirs(plugin_dir: Path, manifest: dict) -> list:
    roots = [plugin_dir / "skills"] + [_inside(plugin_dir, p) for p in _as_list(manifest.get("skills"))]
    found, seen = [], set()
    for root in roots:
        candidates = [root] if (root / "SKILL.md").is_file() else (sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else [])
        for cand in candidates:
            if (cand / "SKILL.md").is_file() and cand.resolve() not in seen:
                seen.add(cand.resolve())
                found.append(cand)
    return found


def hooks_config(plugin_dir: Path, manifest: dict) -> dict:
    """The plugin's hooks as {event: [{matcher, hooks: [{type, command, timeout}]}]}."""
    merged = {}
    sources = []
    default = plugin_dir / "hooks" / "hooks.json"
    if default.is_file():
        sources.append(_read_json(default))
    for item in _as_list(manifest.get("hooks")):
        if isinstance(item, dict):
            sources.append(item)
        elif isinstance(item, str):
            path = _inside(plugin_dir, item)
            if path.resolve() != default.resolve():
                sources.append(_read_json(path))
    for src in sources:
        for event, groups in (src.get("hooks", src) or {}).items():
            if isinstance(groups, list):
                merged.setdefault(event, []).extend(groups)
    return merged


def mcp_config(plugin_dir: Path, manifest: dict) -> dict:
    servers = {}
    default = plugin_dir / ".mcp.json"
    if default.is_file():
        data = _read_json(default)
        servers.update(data.get("mcpServers", data))
    for item in _as_list(manifest.get("mcpServers")):
        if isinstance(item, dict):
            servers.update(item.get("mcpServers", item))
        elif isinstance(item, str):
            data = _read_json(_inside(plugin_dir, item))
            servers.update(data.get("mcpServers", data))
    return servers


def unsupported_parts(plugin_dir: Path, manifest: dict, hooks: dict) -> list:
    notes = []
    if (plugin_dir / "agents").is_dir() or manifest.get("agents"):
        notes.append("agents/ (subagents with their own tools and model): Hermes has no equivalent")
    if manifest.get("outputStyles") or (plugin_dir / "output-styles").is_dir():
        notes.append("output styles: no Hermes equivalent")
    if manifest.get("lspServers") or (plugin_dir / ".lsp.json").is_file():
        notes.append("LSP servers: no Hermes equivalent")
    for event in hooks:
        if event not in SUPPORTED_EVENTS:
            notes.append("%s hooks: no Hermes equivalent, not run" % event)
    return notes


def hooks_digest(hooks: dict) -> str:
    return hashlib.sha256(json.dumps(hooks, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def hook_commands(hooks: dict) -> list:
    out = []
    for event, groups in hooks.items():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                if hook.get("type", "command") == "command" and hook.get("command"):
                    out.append((event, group.get("matcher", ""), hook["command"], hook.get("timeout")))
    return out


# ---- writing the Hermes side ---------------------------------------------

def _frontmatter(text: str):
    """(dict of simple key: value pairs, body). Values are kept as strings; enough for description
    and argument-hint, which is all a command file needs."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            meta[key.strip()] = value
    return meta, text[match.end():]


def _yaml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _rewrite_root(text: str, root: Path) -> str:
    return ROOT_VAR.sub(str(root).replace("\\", "/"), text)


def build_package(plugin_dir: Path, manifest: dict, version: str = "") -> dict:
    """Copy the plugin to $HERMES_HOME/plugins/claude-<name>/ as an Agent Plugins v1 package.
    Returns {"package": path, "skills": [...], "mcp": [...], "notes": [...]}."""
    name = manifest["name"]
    pkg_name = paths.package_name(name)
    if not PORTABLE_NAME.match(pkg_name) or len(pkg_name) > 64:
        raise TranslateError("plugin name %r cannot be a Hermes package name (lowercase letters, digits, dots, hyphens)" % name)
    dest = paths.package_dir(name)
    staging = dest.parent / (".incoming-" + dest.name)
    if staging.exists():
        shutil.rmtree(staging)
    paths.ensure(dest.parent)
    shutil.copytree(plugin_dir, staging, ignore=shutil.ignore_patterns(".git"), symlinks=False)
    notes = []

    # Skills: Hermes reads <package>/skills/<name>/SKILL.md. Copy any extra skill paths in.
    skills = []
    for sdir in skill_dirs(staging, manifest):
        target = staging / "skills" / sdir.name
        if sdir.resolve() != target.resolve():
            if target.exists():
                notes.append("skill %s listed twice; kept the copy under skills/" % sdir.name)
            else:
                shutil.copytree(sdir, target)
        skills.append(sdir.name)
    for skill_md in (staging / "skills").glob("*/SKILL.md") if (staging / "skills").is_dir() else []:
        skill_md.write_text(_rewrite_root(skill_md.read_text(encoding="utf-8"), dest), encoding="utf-8")

    # MCP servers -> portable mcp.json
    portable, mcp_names = {}, []
    for server, cfg in mcp_config(staging, manifest).items():
        translated, why = translate_mcp(cfg)
        if translated is None:
            notes.append("MCP server %s not installed: %s" % (server, why))
            continue
        portable[server] = translated
        mcp_names.append(server)
    if portable:
        with open(staging / "mcp.json", "w", encoding="utf-8") as fh:
            json.dump({"$schema": AGENT_MCP_SCHEMA, "mcpServers": portable}, fh, indent=2)
    elif (staging / "mcp.json").exists():
        (staging / "mcp.json").unlink()

    author = manifest.get("author") or {}
    if isinstance(author, str):
        author = {"name": author}
    package_manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": pkg_name,
        "version": str(version or manifest.get("version") or "0.0.0"),
        "description": ("Claude Code plugin %s, translated by hermes-claude-plugins. %s" % (name, manifest.get("description") or "")).strip(),
        "author": {k: str(v) for k, v in author.items() if k in ("name", "email", "url") and v},
    }
    for key in ("homepage", "license"):
        if isinstance(manifest.get(key), str):
            package_manifest[key] = manifest[key]
    repo = manifest.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    if isinstance(repo, str):
        package_manifest["repository"] = repo
    if isinstance(manifest.get("keywords"), list):
        package_manifest["keywords"] = [str(k) for k in manifest["keywords"]]
    with open(staging / "plugin.json", "w", encoding="utf-8") as fh:
        json.dump(package_manifest, fh, indent=2)

    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return {"package": str(dest), "skills": skills, "mcp": mcp_names, "notes": notes}


def translate_mcp(cfg: dict):
    """(portable entry, None) or (None, reason)."""
    kind = (cfg.get("type") or ("stdio" if cfg.get("command") else "")).lower()
    if kind == "stdio":
        command = str(cfg.get("command") or "")
        match = re.match(r"^(?:\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT)/(.+)$", command)
        if match:
            command = "./" + match.group(1)
        elif "/" in command or " " in command:
            return None, "command %r must be a bare executable or inside the plugin (Agent Plugins v1 rule); add it to config.yaml mcp_servers by hand" % command
        entry = {"type": "stdio", "command": command}
        args = [ROOT_VAR.sub("${PLUGIN_ROOT}", str(a)) for a in cfg.get("args") or []]
        if args:
            entry["args"] = args
        env = {str(k): ROOT_VAR.sub("${PLUGIN_ROOT}", str(v)) for k, v in (cfg.get("env") or {}).items()}
        left = [v for v in list(env.values()) + args if re.search(r"\$\{(?!PLUGIN_ROOT\}|PLUGIN_DATA\})[A-Za-z_]", v)]
        if left:
            return None, "uses environment placeholders (%s) that Hermes packages do not expand; add it to config.yaml mcp_servers by hand" % ", ".join(left[:3])
        if env:
            entry["env"] = env
        return entry, None
    if kind in ("http", "streamable-http"):
        url = str(cfg.get("url") or "")
        headers = cfg.get("headers") or {}
        if "${" in url or any("${" in str(v) for v in headers.values()):
            return None, "uses environment placeholders in its URL or headers; add it to config.yaml mcp_servers by hand"
        entry = {"type": "streamable-http", "url": url}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        return entry, None
    if kind == "sse":
        return None, "SSE transport (Hermes packages accept stdio and streamable-http only)"
    return None, "unknown transport %r" % kind


def command_skill_name(plugin: str, command: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (plugin, command)).lower()).strip("-")
    return slug[:64].rstrip("-")


def build_command_skills(plugin_name: str, commands: list, package_root: Path) -> list:
    """Write one skill per command into the command-skills directory. Returns the skill names."""
    out_root = paths.ensure(paths.command_skills_dir())
    remove_command_skills(plugin_name)
    names = []
    for command, path in commands:
        meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        skill = command_skill_name(plugin_name, command)
        description = meta.get("description") or ("The /%s:%s command from the Claude Code plugin %s." % (plugin_name, command, plugin_name))
        hint = meta.get("argument-hint", "")
        note = (
            "This skill is the `/%s:%s` command from the Claude Code plugin `%s`, translated for Hermes by "
            "hermes-claude-plugins. The plugin's files are in `%s`.\n\n"
            "Wherever the instructions below say `$ARGUMENTS` (or `$1`, `$2`, ...), use the text the user typed "
            "after the command, which Hermes passes along as the user's instruction%s. If there is none, treat it "
            "as empty. Instructions that name Claude Code tools mean the Hermes equivalents: Bash is `terminal`, "
            "Read is `read_file`, Write is `write_file`, Edit is `patch`, Grep and Glob are `search_files`.\n\n---\n\n"
            % (plugin_name, command, plugin_name, package_root, (" (expected: %s)" % hint) if hint else "")
        )
        text = "---\nname: %s\ndescription: %s\nmetadata:\n  claude_plugin: %s\n  claude_command: %s\n---\n\n%s%s" % (
            skill, _yaml_str(description), _yaml_str(plugin_name), _yaml_str(command), note, _rewrite_root(body, package_root))
        target = out_root / skill
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(text, encoding="utf-8")
        (target / ".claude-plugin-owner").write_text(plugin_name + "\n", encoding="utf-8")
        names.append(skill)
    return names


def remove_command_skills(plugin_name: str) -> None:
    root = paths.command_skills_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        owner = child / ".claude-plugin-owner"
        if owner.is_file() and owner.read_text(encoding="utf-8").strip() == plugin_name:
            shutil.rmtree(child)
