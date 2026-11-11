---
description: When and why a behavior became what it is - runs a probe back through git history to the change, then shows the commit, PR link and the bw task (prompt and decisions) behind it (e.g. /bw:why late-fee)
argument-hint: "<probe-id>"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" why $ARGUMENTS`

Above is what bw found by running the probe at earlier commits, and what was written at the point it changed. Show it to the user as is, in a code block. Then add at most three lines, each labelled:
- "Stated:" only words quoted above (commit message, PR link, the prompt of a bw task, a decision), with where they come from.
- "Not found:" what bw searched and found nothing in.
- "Next:" one concrete step if the reason is still unknown (for example: read PR #N, or ask the person who made the commit).
Do not add reasons that are not quoted above. Change nothing.
