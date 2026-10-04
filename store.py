"""The bridge's state file: marketplaces, installed plugins and hook consent."""
import json
import os
import tempfile
import time

from . import paths


def _empty():
    return {"version": 1, "marketplaces": {}, "plugins": {}}


def load() -> dict:
    try:
        with open(paths.state_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty()
    base = _empty()
    base.update({k: v for k, v in data.items() if k in base})
    return base


def save(state: dict) -> None:
    target = paths.state_file()
    paths.ensure(target.parent, private=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".state.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def plugin_key(name: str, marketplace: str) -> str:
    return "%s@%s" % (name, marketplace)


def find_plugin(state: dict, ref: str):
    """Look a plugin up by `name@marketplace` or, when unambiguous, by bare name."""
    plugins = state["plugins"]
    if ref in plugins:
        return ref, plugins[ref]
    matches = [(k, v) for k, v in plugins.items() if v.get("name") == ref]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LookupError("%s is installed from more than one marketplace: %s" % (ref, ", ".join(k for k, _ in matches)))
    raise LookupError("%s is not installed" % ref)
