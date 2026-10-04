"""hermes-claude-plugins: install Claude Code plugins into Hermes and translate them.

Skills and MCP servers become Agent Plugins packages Hermes loads itself, commands become skills
(and so slash commands), and hooks run through Hermes lifecycle hooks once the user accepts them.
"""
import logging

logger = logging.getLogger("hermes_claude_plugins")


def register(ctx):
    from . import cli, hookbridge

    ctx.register_cli_command(
        "claude-plugins", "Install and manage Claude Code plugins in Hermes", cli.setup, cli.handle_cli,
        description="Add Claude Code marketplaces, install their plugins, and translate skills, commands, "
                    "MCP servers and hooks for Hermes.",
    )
    ctx.register_command(
        "claude-plugins", cli.handle_slash,
        description="Manage Claude Code plugins (list, install, update, consent, ...)",
        args_hint="<list | install x@y | show x | ...>",
    )
    bridge = hookbridge.Bridge()
    ctx.register_hook("pre_llm_call", bridge.pre_llm_call)
    ctx.register_hook("on_session_end", bridge.on_session_end)
    ctx.register_hook("pre_tool_call", bridge.pre_tool_call)
    ctx.register_hook("post_tool_call", bridge.post_tool_call)
    ctx.register_hook("on_session_finalize", bridge.on_session_finalize)
