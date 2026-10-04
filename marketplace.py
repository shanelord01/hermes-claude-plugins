"""Claude Code marketplaces: .claude-plugin/marketplace.json and plugin sources."""
import json
import re
from pathlib import Path

from . import fetch, paths


class MarketplaceError(Exception):
    pass


def read(mkt_dir: Path) -> dict:
    path = mkt_dir / ".claude-plugin" / "marketplace.json"
    if not path.is_file():
        raise MarketplaceError("%s has no .claude-plugin/marketplace.json" % mkt_dir)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data.get("plugins"), list):
        raise MarketplaceError("%s: marketplace.json has no plugins list" % path)
    if not data.get("name"):
        raise MarketplaceError("%s: marketplace.json has no name" % path)
    return data


def entry(data: dict, plugin: str) -> dict:
    for item in data["plugins"]:
        if item.get("name") == plugin:
            return item
    raise MarketplaceError("marketplace %s has no plugin %s (it has: %s)" % (
        data.get("name"), plugin, ", ".join(p.get("name", "?") for p in data["plugins"])))


def plugin_dir(mkt_dir: Path, data: dict, item: dict, workdir: Path):
    """Return (directory holding the plugin, revision). Fetches remote plugin sources into workdir."""
    source = item.get("source")
    if isinstance(source, str):
        root = (data.get("metadata") or {}).get("pluginRoot") or ""
        rel = source if source.startswith(("./", "../")) or not root else "%s/%s" % (root.rstrip("/"), source)
        target = (mkt_dir / rel).resolve()
        if mkt_dir.resolve() != target and mkt_dir.resolve() not in target.parents:
            raise MarketplaceError("plugin source %s escapes the marketplace" % source)
        if not target.is_dir():
            raise MarketplaceError("plugin source %s is not a directory in the marketplace" % source)
        return target, ""
    if isinstance(source, dict):
        kind = source.get("source")
        pinned = source.get("sha") or source.get("ref")  # a pinned commit wins over a moving ref
        if kind == "github":
            spec = {"source": "github", "repo": source["repo"], "ref": pinned}
        elif kind in ("url", "git", "git-subdir"):
            gh = re.match(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", source.get("url") or "")
            if gh:
                spec = {"source": "github", "repo": gh.group(1), "ref": pinned}
            else:
                spec = {"source": "git", "url": source["url"], "ref": source.get("ref"), "sha": source.get("sha")}
        else:
            raise MarketplaceError("plugin source type %r is not supported yet" % kind)
        dest = workdir / "plugin"
        rev = fetch.fetch(spec, dest)
        sub = source.get("path")
        if sub:
            target = (dest / sub).resolve()
            if dest.resolve() not in target.parents:
                raise MarketplaceError("plugin path %s escapes the repository" % sub)
            return target, rev
        return dest, rev
    raise MarketplaceError("plugin %s has no usable source" % item.get("name"))


def marketplace_dir(name: str) -> Path:
    return paths.marketplaces_dir() / name
