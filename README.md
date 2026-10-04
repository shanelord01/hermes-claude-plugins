# hermes-claude-plugins

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that installs Claude Code
plugins into Hermes. It adds Claude Code marketplaces, installs their plugins, and translates each
part into what Hermes understands:

| Claude Code plugin part | In Hermes |
|---|---|
| `skills/<name>/SKILL.md` | Skills of an Agent Plugins package that Hermes loads itself |
| `.mcp.json` / `mcpServers` (stdio, HTTP) | MCP servers of that package |
| `commands/*.md` | One skill per command, so `/plugin:command` becomes `/plugin-command` |
| `hooks/hooks.json` | Run through Hermes lifecycle hooks, only after you accept them |
| `agents/`, output styles, LSP servers | Not installed. `show` lists what was left out and why |

Each installed plugin becomes a Hermes package at `$HERMES_HOME/plugins/claude-<name>/`, so it
appears in `hermes plugins list` and can be disabled like any other plugin.

## Install

```
hermes plugins install shanelord01/hermes-claude-plugins --enable
```

Restart the gateway (or start a new CLI session) so the `claude-plugins` command and hooks load.

## Use

```
hermes claude-plugins marketplace add anthropics/claude-plugins-official
hermes claude-plugins install chrome-devtools-mcp
hermes claude-plugins list
hermes claude-plugins show chrome-devtools-mcp
hermes claude-plugins update            # fetch every marketplace again and reinstall
hermes claude-plugins uninstall chrome-devtools-mcp
```

The same commands work inside a session as `/claude-plugins ...`. Start a new session after
installing so Hermes picks up the new skills, commands and MCP servers.

**Marketplace sources** are `owner/repo` (GitHub, fetched as a tarball, no git needed), a git URL,
a URL to a `marketplace.json`, or a local directory. Plugin sources inside a marketplace can be
relative paths, `github`, `url` or `git-subdir` entries, and a pinned `sha` is honoured. Set
`GITHUB_TOKEN` or `GH_TOKEN` for private repositories.

**Copy a Claude Code setup.** On a machine that also runs Claude Code,
`hermes claude-plugins import-claude` adds the same marketplaces and installs the plugins that are
installed for the user there (project-scoped installs are skipped).

## Hooks

Hooks run shell commands, so they never run until you accept them for that plugin:

```
hermes claude-plugins show mempalace-sharedbrain       # lists every hook command
hermes claude-plugins consent mempalace-sharedbrain     # allow them
hermes claude-plugins consent mempalace-sharedbrain --revoke
```

`install --accept-hooks` accepts them at install time. Consent covers the exact hooks you saw: an
update that changes a plugin's hooks switches them off until you accept again.

Each hook runs through `/bin/sh` with the JSON on stdin and the environment Claude Code gives it
(`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PROJECT_DIR`). Hooks that read `transcript_path` get a Claude-format
transcript written from the Hermes conversation.

| Claude Code event | Hermes hook | Behaviour |
|---|---|---|
| SessionStart | `pre_llm_call`, first turn | Its context is added to the first turn |
| UserPromptSubmit | `pre_llm_call` | Its context is added to the turn. Hermes cannot block a prompt, so a block's reason is passed on instead |
| Stop | `on_session_end` | Hermes cannot keep a finished turn going, so a block's reason arrives with the next turn |
| PreToolUse | `pre_tool_call` | `deny`, `ask` or exit code 2 blocks the tool call |
| PostToolUse | `post_tool_call` | Runs for its side effects |
| SessionEnd | `on_session_finalize` | Runs for its side effects |
| PreCompact, Notification, SubagentStop and others | none | Listed by `show`, never run |

Tool matchers use Claude Code names, mapped from Hermes tools: `terminal` is Bash, `read_file` is
Read, `write_file` is Write, `patch` is Edit, `search_files` is Grep. Hook calls are capped at 25
seconds, inside Hermes's 30 second hook limit.

## Limits

- MCP servers must use stdio or streamable HTTP, and a stdio command must be a bare executable or a
  file inside the plugin. Servers that need environment placeholders such as `${API_TOKEN}`, absolute
  commands or SSE are reported and left for you to add to `config.yaml` under `mcp_servers`.
- Command skills keep `$ARGUMENTS` in their text with a note telling the model to use what the user
  typed after the command. Hermes appends that text to the skill rather than substituting it.
- Subagents, `allowed-tools`, `model` and `disable-model-invocation` have no Hermes equivalent.

## Files

```
$HERMES_HOME/claude-plugins/state.json            marketplaces, installed plugins, hook consent
$HERMES_HOME/claude-plugins/marketplaces/<name>/  fetched marketplaces
$HERMES_HOME/claude-plugins/command-skills/       generated command skills (in skills.external_dirs)
$HERMES_HOME/plugins/claude-<plugin>/             each installed plugin as a Hermes package
```

## Tests

```
python3 -m unittest discover -s tests -v
```

They need no network and no Hermes install. When a checkout of
[claude-mempalace-sharedbrain](https://github.com/shanelord01/claude-mempalace-sharedbrain) sits at
`~/projects/claude-mempalace-sharedbrain`, one test installs it through the bridge and runs its
SessionStart hook.

## Licence

MIT
