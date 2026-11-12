---
description: Check bw's setup in this repo (python, git, hooks seen, recent bw errors), or show one bw error by id (e.g. /bw:doctor e1a2)
argument-hint: "[error id]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" doctor $ARGUMENTS`

Show the user what bw printed above, exactly as printed, in a code block. If a line starts with FIX, add one short line on how to fix it. Do nothing else.
