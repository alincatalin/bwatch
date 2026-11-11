---
description: Turn bw on for this repo (drafts a map from the folders, or keeps the existing one)
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" on`

Tell the user in two short lines what bw printed above: that bw is on, how many regions the map has, and the link to the watch room (the file:// line). Do nothing else.
