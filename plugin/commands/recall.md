---
description: Where things stand in one area before you start - findings (open, picked, fixed), your beliefs (holding, broken, can't tell, flipped back), changes nobody asked for, last tasks (e.g. /bw:recall Billing or /bw:recall src/billing)
argument-hint: "[region or path]"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" recall $ARGUMENTS`

Show the user what bw printed above, as is, in a code block. Do nothing else.
