"""`hermes claude-plugins ...` and the `/claude-plugins ...` slash command share one parser."""
import argparse
import shlex

from . import ops

USAGE = """\
hermes claude-plugins marketplace add <owner/repo | git URL | URL to marketplace.json | path> [--name N]
hermes claude-plugins marketplace list | update [name] | remove <name>
hermes claude-plugins install <plugin>[@<marketplace>] [--accept-hooks]
hermes claude-plugins consent <plugin> [--revoke]
hermes claude-plugins list | show <plugin> | update [plugin] | uninstall <plugin> | sync
hermes claude-plugins import-claude [--claude-dir ~/.claude] [--accept-hooks]"""


def setup(parser: argparse.ArgumentParser) -> None:
    parser.description = "Install Claude Code plugins into Hermes and translate them."
    sub = parser.add_subparsers(dest="cp_command")

    m = sub.add_parser("marketplace", help="add, list, update or remove Claude Code marketplaces")
    msub = m.add_subparsers(dest="cp_mkt_command")
    a = msub.add_parser("add")
    a.add_argument("source")
    a.add_argument("--name")
    msub.add_parser("list")
    u = msub.add_parser("update")
    u.add_argument("name", nargs="?")
    r = msub.add_parser("remove")
    r.add_argument("name")

    i = sub.add_parser("install", help="install a plugin from an added marketplace")
    i.add_argument("plugin")
    i.add_argument("--accept-hooks", action="store_true", help="allow the plugin's hooks to run shell commands")
    i.add_argument("--no-enable", action="store_true")

    c = sub.add_parser("consent", help="allow (or with --revoke, stop) a plugin's hooks")
    c.add_argument("plugin")
    c.add_argument("--revoke", action="store_true")

    sub.add_parser("list", help="installed plugins")
    s = sub.add_parser("show", help="what a plugin installed and what could not be translated")
    s.add_argument("plugin")
    up = sub.add_parser("update", help="fetch the marketplace again and reinstall")
    up.add_argument("plugin", nargs="?")
    un = sub.add_parser("uninstall")
    un.add_argument("plugin")
    sub.add_parser("sync", help="re-translate every installed plugin from the cached marketplaces")
    ic = sub.add_parser("import-claude", help="install the marketplaces and user-wide plugins of a Claude Code setup")
    ic.add_argument("--claude-dir", default="~/.claude")
    ic.add_argument("--accept-hooks", action="store_true")


def run(args) -> list:
    cmd = getattr(args, "cp_command", None)
    if cmd == "marketplace":
        mc = getattr(args, "cp_mkt_command", None)
        if mc == "add":
            return [ops.marketplace_add(args.source, args.name)]
        if mc == "list":
            return ops.marketplace_list() or ["no marketplaces added"]
        if mc == "update":
            return ops.marketplace_update(args.name)
        if mc == "remove":
            return [ops.marketplace_remove(args.name)]
        return [USAGE]
    if cmd == "install":
        return ops.install(args.plugin, accept_hooks=args.accept_hooks, enable=not args.no_enable)
    if cmd == "consent":
        return ops.consent(args.plugin, revoke=args.revoke)
    if cmd == "list":
        return ops.plugin_list()
    if cmd == "show":
        return ops.show(args.plugin)
    if cmd == "update":
        return ops.update(args.plugin)
    if cmd == "uninstall":
        return ops.uninstall(args.plugin)
    if cmd == "sync":
        return ops.sync()
    if cmd == "import-claude":
        return ops.import_claude(args.claude_dir, accept_hooks=args.accept_hooks)
    return [USAGE]


def _safe_run(args) -> str:
    try:
        return "\n".join(run(args))
    except Exception as exc:
        return "claude-plugins: %s" % exc


def handle_cli(args) -> int:
    text = _safe_run(args)
    print(text)
    return 1 if text.startswith("claude-plugins: ") else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def handle_slash(raw_args: str) -> str:
    parser = _Parser(prog="/claude-plugins", add_help=False)
    setup(parser)
    try:
        args = parser.parse_args(shlex.split(raw_args or "list"))
    except (ValueError, SystemExit) as exc:
        return "%s\n\n%s" % (exc, USAGE)
    return _safe_run(args)
