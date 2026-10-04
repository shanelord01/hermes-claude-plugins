# hermes-claude-plugins installed

Restart the gateway (`hermes gateway restart`) or start a new CLI session, then:

    hermes claude-plugins marketplace add anthropics/claude-plugins-official
    hermes claude-plugins install <plugin>

Hooks from a Claude Code plugin never run until you accept them with
`hermes claude-plugins consent <plugin>`.
