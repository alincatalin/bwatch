---
description: Show the bw map, or mark a region as cheap, fix, costly or critical (e.g. /bw:map Billing critical)
argument-hint: "[region] [cheap|fix|costly|critical]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" map $ARGUMENTS`

Show the user what bw printed above, as is, in a code block. Do nothing else.
