"""Where the bridge keeps things inside HERMES_HOME.

    $HERMES_HOME/claude-plugins/state.json            marketplaces, installed plugins, hook consent
    $HERMES_HOME/claude-plugins/marketplaces/<name>/  fetched marketplace trees
    $HERMES_HOME/claude-plugins/command-skills/       one generated skill per Claude command
                                                      (registered in skills.external_dirs)
    $HERMES_HOME/claude-plugins/transcripts/          Claude-format transcripts for hooks that read one
    $HERMES_HOME/plugins/claude-<plugin>/             each installed plugin as an Agent Plugins v1
                                                      package (skills and MCP servers, loaded by Hermes)
"""
import os
from pathlib import Path

PACKAGE_PREFIX = "claude-"


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def root() -> Path:
    return hermes_home() / "claude-plugins"


def state_file() -> Path:
    return root() / "state.json"


def marketplaces_dir() -> Path:
    return root() / "marketplaces"


def command_skills_dir() -> Path:
    return root() / "command-skills"


def transcripts_dir() -> Path:
    return root() / "transcripts"


def packages_dir() -> Path:
    return hermes_home() / "plugins"


def package_name(plugin_name: str) -> str:
    return PACKAGE_PREFIX + plugin_name


def package_dir(plugin_name: str) -> Path:
    return packages_dir() / package_name(plugin_name)


def ensure(path: Path, private: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path
