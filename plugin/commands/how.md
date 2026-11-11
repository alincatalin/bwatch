---
description: Investigate read-only: answer a question about the code with probes that show it running, before anything is changed (e.g. /bw:how what happens when two requests update the same record?)
argument-hint: "<question>"
---
The engineer asked: $ARGUMENTS

This is a read-only investigation. Do not change any code; bw refuses edits outside `.bw/probes/` in this task.

1. Read the code that answers the question. Trace it from the way a user gets in (a screen, a command, an API route) to the outcome.
2. For each thing that matters in your answer (how it works, a gotcha, a bug, a surprise), write a probe that shows it on the code as it is, following the bw-probes skill: `.bw/probes/<id>/probe.json` with a `finding` (one line, in the user's words), `scenario`, `cmd`, `covers`. At most 5.
3. Check them with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" receipt --from HEAD`.
4. Answer in a few short lines. Cite file:line for each claim. Mark anything no probe showed with a line starting `NOT SHOWN:` and say why.

bw runs the probes when you finish and puts each finding in front of the engineer to Fix, mark Expected, or Skip.
