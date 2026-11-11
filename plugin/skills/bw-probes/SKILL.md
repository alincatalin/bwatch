---
name: bw-probes
description: Use when writing or editing a bw behavior probe in .bw/probes/ - after bw asks for probes, in a review or /bw:how task, or when changing behavior in a repo where bw is on.
---

# Writing bw probes

A probe runs one situation through the real code and prints what happens. bw runs it on a clean copy of the code at two commits and shows the engineer the two outputs. Your words are never shown as evidence, only what the probe printed. A probe is only as good as the break it would show.

## File

`.bw/probes/<id>/probe.json`, with any script beside it:

```json
{"scenario": "who does what, with which inputs; never the outcome",
 "cmd": "PYTHONPATH=src python3 .bw/probes/<id>/run.py",
 "covers": ["src/billing/fees.py"],
 "entry": "optional: how a user gets there, e.g. Settings > Plan > Cancel, `app export --csv`, POST /invoices",
 "finding": "optional, review tasks only: one line, what is wrong or surprising, in the user's words",
 "role": "optional: \"safety\" for the one fact this change is safe because of",
 "functions": ["optional: the functions the probe drives, so `bwcap.py hollow` plants breaks there"]}
```

## Rules

1. **Drive the behavior the way its users do.** Call the public function, command, route or screen logic a caller uses. Do not call private helpers or read internal state: a probe pinned to internals breaks on every refactor and shows nothing about behavior.
2. **Print what a user can observe.** Return values, messages, files written, rows saved, requests sent, exit codes. Not which internal function ran.
3. **Use a small table of inputs.** 3 to 6 labelled lines: a typical case, both sides of every condition you changed or are asking about, and the error path. One line per input: `late 4 days, 1000 -> 20.0`.
4. **Choose inputs where a break shows.** Ask: if someone flipped this comparison or dropped this branch, would a line of my output change? If not, pick other inputs. `bwcap.py hollow <id>` plants such breaks and tells you.
5. **Deterministic and fast.** No timestamps, random ids, absolute paths, network or timing. Under 40 lines, a few seconds.
6. **Load the code from the copy bw runs in** (the current directory), never from an installed package or another checkout. Use the language and tools the repo already uses.
7. **Never edit a probe to make its output match.** Add a new probe instead. bw keeps its own copy of every probe the engineer kept.

## In a review or /bw:how task

Write one probe per finding that prints the problem itself (the wrong value, the cut line, the error), so that after a fix the same probe prints something else. Put the finding in `finding`. If a finding needs a device, network or UI and no probe can show it, write `NOT SHOWN: <finding> - <reason>` in your reply. Do not change code outside `.bw/probes/`.

## When fixing a finding the engineer picked

bw's brief lists the finding probes under CHANGE. Fix the cause, not the reported path only: if the behavior has another way in (`entry`), add a probe for that way too, so the receipt shows both changed. Leave the finding probe as it is.

## Check before you finish

`python3 <bw>/bwcap.py receipt --from <commit before your change>` shows every probe before and after.
