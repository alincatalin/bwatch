---
description: Open a Markdown plan file as a bw plan page and wait for your approval or changes (e.g. /bw:plan docs/PLAN.md)
argument-hint: "<plan.md>"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" plan open $ARGUMENTS`

Above, bw opened the plan file as a page for the engineer. If it printed an error, tell the user in one line and stop. Otherwise follow its numbered steps exactly: give the user the link in one line, then start the wait command with run_in_background: true. When the wait returns, do what it says.
