---
description: Do the changes you queued in the bw watch room with "Do it now"
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" do`

Above are the engineer's requests from the bw watch room, each with the code lines it is about. If it says no requests are waiting, tell the user that in one line and stop. Otherwise carry out each request in the code: read the cited lines first, make the smallest change that does what was asked, and run the relevant tests if there are any. For a "check" request, answer from the code with file:line and change nothing. End with one short line per request: done, or not done and why.
