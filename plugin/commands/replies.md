---
description: Print your latest answer on a bw plan page (for when the agent was not waiting)
argument-hint: "[plan id]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" plan reply $ARGUMENTS`

Above is the engineer's latest answer on the bw plan page. If it says there is none, tell the user in one line and stop. Otherwise act on it as its first line says.
