"""The operations behind `hermes claude-plugins` and `/claude-plugins`, mirroring `claude plugin`."""
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import fetch, hermes_glue, marketplace as M, paths, store, translate


class OpError(Exception):
    pass


# ---- marketplaces ---------------------------------------------------------

def marketplace_add(source_text: str, name: str = None) -> str:
    source = fetch.parse_source(source_text)
    state = store.load()
    with tempfile.TemporaryDirectory(prefix="hermes-claude-mkt-") as tmp:
        staged = Path(tmp) / "m"
        rev = fetch.fetch(source, staged)
        data = M.read(staged)
        mkt_name = name or data["name"]
        if mkt_name in state["marketplaces"] and state["marketplaces"][mkt_name].get("source") != source:
            raise OpError("a marketplace named %s already exists from %s; remove it first or pass --name" % (
                mkt_name, fetch.describe(state["marketplaces"][mkt_name]["source"])))
        fetch._replace_dir(staged, M.marketplace_dir(mkt_name))
    state["marketplaces"][mkt_name] = {"source": source, "revision": rev, "updated_at": store.now()}
    store.save(state)
    return "added marketplace %s from %s (%d plugins: %s)" % (
        mkt_name, fetch.describe(source), len(data["plugins"]), ", ".join(p.get("name", "?") for p in data["plugins"]))


def marketplace_update(name: str = None) -> list:
    state = store.load()
    names = [name] if name else sorted(state["marketplaces"])
    out = []
    for mkt in names:
        rec = state["marketplaces"].get(mkt)
        if not rec:
            raise OpError("no marketplace named %s" % mkt)
        rev = fetch.fetch(rec["source"], M.marketplace_dir(mkt))
        M.read(M.marketplace_dir(mkt))
        rec.update(revision=rev, updated_at=store.now())
        out.append("updated marketplace %s%s" % (mkt, (" to %s" % rev) if rev else ""))
    store.save(state)
    return out


def marketplace_remove(name: str) -> str:
    state = store.load()
    if name not in state["marketplaces"]:
        raise OpError("no marketplace named %s" % name)
    users = [k for k, v in state["plugins"].items() if v.get("marketplace") == name]
    if users:
        raise OpError("uninstall these first: %s" % ", ".join(users))
    state["marketplaces"].pop(name)
    shutil.rmtree(M.marketplace_dir(name), ignore_errors=True)
    store.save(state)
    return "removed marketplace %s" % name


def marketplace_list() -> list:
    state = store.load()
    out = []
    for name, rec in sorted(state["marketplaces"].items()):
        try:
            plugins = [p.get("name") for p in M.read(M.marketplace_dir(name))["plugins"]]
        except Exception as exc:
            plugins = ["(unreadable: %s)" % exc]
        out.append("%s  %s  updated %s  plugins: %s" % (name, fetch.describe(rec["source"]), rec.get("updated_at", "?"), ", ".join(plugins)))
    return out


# ---- plugins --------------------------------------------------------------

def _split_ref(state: dict, ref: str):
    if "@" in ref:
        plugin, mkt = ref.rsplit("@", 1)
        return plugin, mkt
    hits = []
    for mkt in state["marketplaces"]:
        try:
            if any(p.get("name") == ref for p in M.read(M.marketplace_dir(mkt))["plugins"]):
                hits.append(mkt)
        except Exception:
            continue
    if len(hits) == 1:
        return ref, hits[0]
    if not hits:
        raise OpError("no added marketplace offers %s; add one with `hermes claude-plugins marketplace add <source>`" % ref)
    raise OpError("%s is offered by %s; use %s@<marketplace>" % (ref, ", ".join(hits), ref))


def describe_hooks(hooks: dict) -> list:
    return ["%s%s: %s%s" % (event, (" [%s]" % matcher) if matcher else "", command,
                            "" if event in translate.SUPPORTED_EVENTS else "  (no Hermes equivalent, never run)")
            for event, matcher, command, _t in translate.hook_commands(hooks)]


def install(ref: str, accept_hooks: bool = False, enable: bool = True) -> list:
    state = store.load()
    plugin, mkt = _split_ref(state, ref)
    if mkt not in state["marketplaces"]:
        raise OpError("marketplace %s has not been added" % mkt)
    mkt_dir = M.marketplace_dir(mkt)
    data = M.read(mkt_dir)
    item = M.entry(data, plugin)
    with tempfile.TemporaryDirectory(prefix="hermes-claude-plugin-") as tmp:
        pdir, rev = M.plugin_dir(mkt_dir, data, item, Path(tmp))
        manifest = translate.load_manifest(pdir, item)
        if manifest["name"] != plugin:
            raise OpError("marketplace entry %s points at a plugin named %s" % (plugin, manifest["name"]))
        version = str(manifest.get("version") or item.get("version") or rev or "0.0.0")
        built = translate.build_package(pdir, manifest, version)
    package_root = Path(built["package"])
    manifest = translate.load_manifest(package_root, item)
    hooks = translate.hooks_config(package_root, manifest)
    commands = translate.command_files(package_root, manifest)
    command_skills = translate.build_command_skills(plugin, commands, package_root)
    notes = built["notes"] + translate.unsupported_parts(package_root, manifest, hooks)

    key = store.plugin_key(plugin, mkt)
    previous = state["plugins"].get(key, {})
    digest = translate.hooks_digest(hooks)
    consented = previous.get("consented_digest") if previous.get("hooks_consent") else None
    if accept_hooks and hooks:
        consented = digest
    rec = {
        "name": plugin, "marketplace": mkt, "version": version, "revision": rev, "package": str(package_root),
        "package_name": paths.package_name(plugin), "skills": built["skills"], "mcp": built["mcp"],
        "commands": [c for c, _p in commands], "command_skills": command_skills,
        "hooks": hooks, "hooks_digest": digest,
        "hooks_consent": bool(hooks) and consented == digest, "consented_digest": consented if consented == digest else None,
        "notes": notes, "installed_at": previous.get("installed_at") or store.now(), "updated_at": store.now(),
    }
    state["plugins"][key] = rec
    store.save(state)

    report = ["installed %s %s as Hermes package %s" % (key, version, rec["package_name"])]
    report.append("  skills: %s" % (", ".join(built["skills"]) or "none"))
    report.append("  MCP servers: %s" % (", ".join(built["mcp"]) or "none"))
    report.append("  commands: %s" % (", ".join("/%s (was /%s:%s)" % (s, plugin, c) for s, (c, _p) in zip(command_skills, commands)) or "none"))
    if hooks:
        state_txt = "ACCEPTED" if rec["hooks_consent"] else "NOT RUN until you accept them"
        report.append("  hooks (%s):" % state_txt)
        report.extend("    " + line for line in describe_hooks(hooks))
        if not rec["hooks_consent"]:
            report.append("  These run shell commands on this machine. To allow them: hermes claude-plugins consent %s" % key)
    for note in notes:
        report.append("  not translated: %s" % note)
    if command_skills:
        report.append("  " + hermes_glue.register_command_skills_dir()[1])
    if enable:
        report.append("  " + hermes_glue.set_package_enabled(rec["package_name"], True)[1])
    report.append("  Start a new Hermes session (or restart the gateway) to load skills, commands and MCP servers.")
    return report


def consent(ref: str, revoke: bool = False) -> list:
    state = store.load()
    key, rec = store.find_plugin(state, ref)
    hooks = rec.get("hooks") or {}
    if not hooks:
        return ["%s has no hooks" % key]
    if revoke:
        rec.update(hooks_consent=False, consented_digest=None)
        store.save(state)
        return ["hooks of %s will no longer run" % key]
    rec.update(hooks_consent=True, consented_digest=translate.hooks_digest(hooks))
    store.save(state)
    return ["hooks of %s will run in Hermes:" % key] + ["  " + line for line in describe_hooks(hooks)]


def uninstall(ref: str) -> list:
    state = store.load()
    key, rec = store.find_plugin(state, ref)
    out = [hermes_glue.set_package_enabled(rec["package_name"], False)[1]]
    shutil.rmtree(rec["package"], ignore_errors=True)
    translate.remove_command_skills(rec["name"])
    state["plugins"].pop(key)
    store.save(state)
    out.insert(0, "uninstalled %s" % key)
    return out


def update(ref: str = None) -> list:
    state = store.load()
    keys = [store.find_plugin(state, ref)[0]] if ref else sorted(state["plugins"])
    out = []
    refreshed = set()
    for key in keys:
        mkt = state["plugins"][key]["marketplace"]
        if mkt not in refreshed:
            out.extend(marketplace_update(mkt))
            refreshed.add(mkt)
        out.extend(install(key, accept_hooks=False, enable=False))
    return out


def sync() -> list:
    """Re-translate every installed plugin from the cached marketplaces, without fetching."""
    state = store.load()
    out = []
    for key in sorted(state["plugins"]):
        out.extend(install(key, accept_hooks=False, enable=False))
    return out or ["nothing installed"]


def plugin_list() -> list:
    state = store.load()
    out = []
    for key, rec in sorted(state["plugins"].items()):
        hooks = rec.get("hooks") or {}
        hook_txt = "no hooks" if not hooks else ("hooks on" if rec.get("hooks_consent") else "hooks off (not accepted)")
        out.append("%s %s  skills %d, commands %d, MCP %d, %s" % (
            key, rec.get("version"), len(rec.get("skills") or []), len(rec.get("command_skills") or []),
            len(rec.get("mcp") or []), hook_txt))
    return out or ["no Claude Code plugins installed"]


def show(ref: str) -> list:
    state = store.load()
    key, rec = store.find_plugin(state, ref)
    out = ["%s %s (Hermes package %s at %s)" % (key, rec.get("version"), rec.get("package_name"), rec.get("package"))]
    out.append("  skills: %s" % (", ".join(rec.get("skills") or []) or "none"))
    out.append("  MCP servers: %s" % (", ".join(rec.get("mcp") or []) or "none"))
    out.append("  commands: %s" % (", ".join("/" + s for s in rec.get("command_skills") or []) or "none"))
    hooks = rec.get("hooks") or {}
    if hooks:
        out.append("  hooks (%s):" % ("accepted" if rec.get("hooks_consent") else "not accepted"))
        out.extend("    " + line for line in describe_hooks(hooks))
    for note in rec.get("notes") or []:
        out.append("  not translated: %s" % note)
    return out


def import_claude(claude_dir: str = "~/.claude", accept_hooks: bool = False) -> list:
    """Replicate the marketplaces and user-scope plugins of a Claude Code installation."""
    base = Path(os.path.expanduser(claude_dir)) / "plugins"
    try:
        known = json.loads((base / "known_marketplaces.json").read_text(encoding="utf-8"))
        installed = json.loads((base / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpError("cannot read Claude Code plugin records under %s: %s" % (base, exc))
    out = []
    state = store.load()
    for name, rec in sorted(known.items()):
        src = rec.get("source") or {}
        kind = src.get("source")
        if kind == "github":
            text = src["repo"] + (("#" + src["ref"]) if src.get("ref") else "")
        elif kind in ("git", "url"):
            text = src["url"]
        elif kind == "directory":
            text = src["path"]
        else:
            out.append("skipped marketplace %s: source type %r" % (name, kind))
            continue
        if name in state["marketplaces"]:
            out.append("marketplace %s already added" % name)
            continue
        try:
            out.append(marketplace_add(text, name=name))
        except Exception as exc:
            out.append("marketplace %s failed: %s" % (name, exc))
    for key, entries in sorted((installed.get("plugins") or {}).items()):
        scopes = {e.get("scope") for e in entries}
        if "user" not in scopes:
            out.append("skipped %s (installed for a project, not user-wide)" % key)
            continue
        try:
            out.extend(install(key, accept_hooks=accept_hooks))
        except Exception as exc:
            out.append("%s failed: %s" % (key, exc))
    return out
