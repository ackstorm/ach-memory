#!/usr/bin/env sh
# Antigravity SessionStart: runs session-start.sh in the session's folder and hands its text
# to the model as a system message.
#
# Antigravity runs plugin hooks in the plugin folder and names the session folder only in the
# stdin JSON (`"cwd"`). It ignores plain stdout: the result is protojson of
# SessionStartHookResult, `{"injectSteps":[{"systemMessage":{"systemMessage":"..."}}]}`.
# tr, sed and awk, not jq or python: nothing has to be installed before memory works. The
# escaping is sed's, not awk gsub's, whose backslash handling differs between awks. Control
# characters other than tab and newline are dropped: JSON forbids them raw.
# ponytail: a session path holding `"` or `\` is not unescaped; the hook then stays in the
# plugin folder and loads no project context.
set -u
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cwd=$(sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$cwd" ] && cd -- "$cwd" 2>/dev/null
tab=$(printf '\t')
printf '{"injectSteps":[{"systemMessage":{"systemMessage":"'
"$here/session-start.sh" | tr -d '\000-\010\013-\037' \
  | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/$tab/\\\\t/g" \
  | awk 'BEGIN { ORS = "" } { print (NR > 1 ? "\\n" : "") $0 }'
printf '"}}]}\n'
exit 0
