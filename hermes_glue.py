"""The few places the bridge touches Hermes's own configuration. Each falls back to telling the
user the command to run when the Hermes internals it relies on are not importable."""
from . import paths


def register_command_skills_dir():
    """Add the command-skills directory to skills.external_dirs. Returns (changed, message)."""
    target = str(paths.ensure(paths.command_skills_dir()))
    try:
        from hermes_cli.config import load_config_readonly, save_config
    except Exception:
        return False, ("Add %s to skills.external_dirs in config.yaml so translated commands appear as "
                       "slash commands." % target)
    cfg = load_config_readonly() or {}
    skills = cfg.get("skills") or {}
    current = [str(d) for d in (skills.get("external_dirs") or []) if d]
    if target in current:
        return False, "command skills directory already registered"
    save_config({"skills": {"external_dirs": current + [target]}}, merge_existing=True)
    return True, "registered %s in skills.external_dirs" % target


def set_package_enabled(package: str, enable: bool):
    """Enable or disable a generated Agent Plugins package. Returns (ok, message)."""
    try:
        from hermes_cli.plugins_cmd import _set_plugin_enabled
        _set_plugin_enabled(package, enable=enable)
        return True, "%s %s" % ("enabled" if enable else "disabled", package)
    except Exception as exc:
        return False, "run `hermes plugins %s %s` (%s)" % ("enable" if enable else "disable", package, exc)
