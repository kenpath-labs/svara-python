#!/bin/sh
# Claude Code plugin hook (SessionStart). When the user has saved a Svara API
# key in the plugin's settings (userConfig "svara_api_key", kept in the system
# keychain), expose it to the session's Bash commands as SVARA_API_KEY, the
# variable the SDK, the CLI and the skills' scripts read. A SVARA_API_KEY the
# user already exported wins. Prints nothing; never fails the session.

key="${CLAUDE_PLUGIN_OPTION_SVARA_API_KEY:-}"
[ -n "$key" ] || exit 0
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
[ -z "${SVARA_API_KEY:-}" ] || exit 0

# Single-quote the value so no character in it is interpreted by the shell.
quoted=$(printf '%s' "$key" | sed "s/'/'\\\\''/g")
printf "export SVARA_API_KEY='%s'\n" "$quoted" >> "$CLAUDE_ENV_FILE"
exit 0
