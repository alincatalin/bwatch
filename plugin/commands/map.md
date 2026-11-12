---
description: Show which areas make bw ask for probes, or change one (e.g. /bw:map Billing critical, /bw:map 7 costly)
argument-hint: "[number or region] [cheap|normal|costly|critical]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" map $ARGUMENTS`

Show the user what bw printed above, as is, in a code block. Do nothing else.
