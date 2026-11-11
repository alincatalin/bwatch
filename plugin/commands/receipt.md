---
description: Run this repo's behavior probes on the code before and after your changes (default HEAD -> working tree), e.g. /bw:receipt or /bw:receipt --from main
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" receipt $ARGUMENTS`

Show the user what bw printed above, as is, in a code block. Do nothing else.
