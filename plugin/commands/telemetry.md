---
description: bw's opt-in usage counts - status, on, off, show (what would be sent), log (what was sent)
argument-hint: "[on|off|show|log]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" telemetry $ARGUMENTS`

Show the user what bw printed above, exactly as printed, in a code block. Do nothing else.
