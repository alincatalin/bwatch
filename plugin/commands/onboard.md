---
description: Onboard bw to this project - scan structure, history, tests and docs, then write and verify probes for the most important behaviors (about 10-15 minutes, changes no code)
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" onboard scan`

You are onboarding bw to this project. Above is bw's scan, made from git and the files only. Your job is to give bw a checked starting point: probes that show what the most important parts of this project do when they run. This task is read-only: do not change any code. bw refuses edits outside `.bw/`.

1. Read the files listed under DOCS (AGENTS.md, CLAUDE.md, README and the rest), so you know how the project is meant to work and how it is run.
2. For each area under FOCUS, in order, read its entry points and most-changed files. Find the 2 to 4 behaviors in that area that matter most to the people who use the project (a request handled, a record saved, a command's output, a calculation, an error path). For each one write a probe, following the bw-probes skill: `.bw/probes/<id>/probe.json` with
   - `"finding"`: one line, in the user's words, saying what the probe shows (for example "An order with a past delivery date is rejected with 'invalid date'"),
   - `"scenario"`, `"cmd"`, `"covers"` as usual (`covers` lists the files whose code the probe runs),
   - `"area"`: the area name exactly as the scan prints it,
   - `"entry"`: `"path:line"` of the way in a user takes (the route, the command, the public function).
   Use a table of 3 to 6 labelled inputs in each probe, including both sides of the important condition and one error case.
3. Rules from the docs: where a doc states something the code must do (a limit, a format, an ordering, a forbidden case), quote that one line exactly and write a probe that shows what the code does about it, with `"kind": "rule"`, `"quote": "<the exact line>"` and `"source": "<doc path>"`. At most 4 of these.
4. At most 20 probes in total. Never install packages or touch the network. If an area cannot run without a device, a network or a UI, say so in one line instead of writing a probe for it.
5. Write `.bw/project.json`: `{"areas": [{"area": "<area name>", "summary": "<one sentence: what this area does for its users>", "entry": "path:line"}]}`, one entry per FOCUS area.
6. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bwcap.py" onboard verify`. If it lists probes as "not used", fix each one once (or delete it) and run verify again.
7. Finish with at most 5 short lines: how many probes passed, which areas they cover, anything you could not probe, and the onboarding page link that verify printed.
