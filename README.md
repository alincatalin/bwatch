# bw

bw shows what an agent's change does, executed, and tells you when something you rely on stops being true.
It works in any git repo and any language, with Claude Code (plugin hooks) or any other agent (a few CLI commands).

- **Probes.** A probe is a tiny command that runs one situation through the real code and prints what happens, for example
  `PYTHONPATH=src python3 .bw/probes/late-fee/run.py` printing `late_fee(4 days, 1000) -> 20.0`. Probes live in the repo under
  `.bw/probes/<id>/probe.json` (scenario, cmd, covers) with any script beside it. Agents write them; you can too.
- **Receipt.** At the end of a task bw runs every probe on a clean copy of the code before the task and after it, and shows
  the two outputs side by side. Nothing the agent wrote about its change is shown as evidence, only what the code printed.
  If a region you marked costly changed and no probe shows it, bw asks the agent for probes once. If none is possible the
  catch-up says "No executed evidence for a costly change".
- **Beliefs.** "Keep as expected" on a receipt stores that output as your belief, with its own copy of the probe. bw re-runs
  your beliefs after every later task, and only you are told when one breaks: "I asked for this" makes the new output
  your belief, "Not intended" sends the agent a fix request. Before an agent edits the files a belief covers, bw shows it
  the outcome you kept.

- **Review findings (0.13.0).** Ask for a review ("code review the changes") or use `/bw:how <question>`. When the task ends
  with no code changed, bw asks the agent once to show each finding with a probe that runs on the code as it is. The watch
  room lists the findings with what the code printed: **Fix this**, **Expected** (the output becomes your belief) or **Skip**.
  Findings no probe could show are listed apart as the agent's words. `/bw:how` refuses code edits.
- **Brief.** The next task you start, whatever you type ("fix the most important ones", `/bw:do`), gets a short brief
  built only from your decisions: CHANGE (the findings you picked, with their probes), PRESERVE (outcomes you kept near
  that code), AVOID (changes you sent back before), RISK (files in scope no kept outcome covers).
- **Receipt by intent.** The receipt says, for each finding you picked, whether its probe changed (fixed / not fixed), and
  marks outputs that changed without anyone asking. A probe that could not run is "can't tell", never broken or holding.
- **Why and recall.** `/bw:why <probe>` runs the probe back through git history to the commit where its output became what
  it is, and shows the commit message, PR links and the bw task (your prompt, your decision) behind it. `/bw:recall [area]`
  gives one area's state from bw's records. Belief history is append-only.

The watch room keeps the map, timeline, test-run marks, plan pages and steering from earlier versions.

## Install (one command)

Unzip, then from this folder:

    bash install.sh /path/to/your/repo

That installs the plugin, turns bw on for that repo and drafts a map from its folders.
Restart Claude Code in the repo. Needs claude, python3 and git. Keep this folder: the plugin is installed from it.

If you already have a hand-made map, pass it instead of a draft:

    bash install.sh /path/to/your/repo your.map.json

## Use

Work as usual. When the agent finishes, the watch room opens (macOS) and its link appears in the chat.
If the task touched a region marked `migrate` or `answer` and no probe shows a change, the agent first writes probes (about a minute).

- Fix the map: edit `.git/bw/map.json` in the repo (names, and class undo / repair / migrate / answer).
- Watch room any time: open `.git/bw/home.html`. Each task's own debrief is linked from it.
- What you've seen and your decisions are kept in your browser for now, not in the repo.
- Turn off: `python3 plugin/scripts/bwcap.py off /path/to/repo`. In repos where bw is not on, it does nothing.
- Uninstall: `claude plugin uninstall bw@bw-local`.

Nothing is committed and nothing leaves your machine. State lives in `.git/bw/`.

## Other agents, git hooks, CI

The same pieces work without Claude Code hooks. Run them from the repo (`BW=path/to/plugin/scripts/bwcap.py`):

    python3 $BW on                                  # once per repo
    python3 $BW agents-md >> AGENTS.md              # tells any agent how to write probes and mark tasks
    python3 $BW task start "what was asked"         # before the agent changes code
    python3 $BW task end                            # after; exit 3 = bw asks for probes, write them, run it again
    python3 $BW receipt [--from REF] [--to REF]     # probes on two versions (default HEAD -> working tree); --json
    python3 $BW keep <probe-id>                     # keep the current output as your belief, no browser
    python3 $BW beliefs [--check [REF]]             # list, or re-run (exit 1 if one broke)

Git hook (pre-push): `python3 $BW beliefs --check HEAD || exit 1`. CI: `python3 $BW receipt --from origin/main --to HEAD`
prints what the branch changes, for every probe committed in the repo (beliefs stay on your machine).

Probe environment: the command runs in the root of a clean copy (`git archive`) of the code at each commit, with
`TZ=UTC`, `NO_COLOR=1`, `PYTHONHASHSEED=0`. Installed dependency folders (`node_modules`, `.venv`, `venv`, `vendor`, `Pods`,
`.bundle`, `deps`, `_build`) are linked in from your checkout; build outputs are not, since they hold the current code.
Anything else a copy needs goes in `.bw/probe-setup.sh`, run once per copy. Outputs are compared after removing
timestamps, durations, memory addresses and temp paths; each probe runs twice after the change and a probe that prints
different output on each run is marked flaky and never counted as a change. Limits: `BW_PROBE_TIMEOUT` (30 s per run) and
`BW_PROBE_BUDGET` (120 s per receipt). `BW_ASK_PROBES=0` stops bw asking the agent for probes.

## Cloud threads

The watch room runs on 127.0.0.1, which a cloud session can't share. When bw runs on the repo named by `BW_REPO` it also writes a static snapshot to `<repo>/watch.html` after every task (or to the file `BW_PAGE` names). Decide findings by writing `fix finding <name>`, `expected finding <name>` or `skip finding <name>` in chat: bw records it from the prompt, even inside the hosted session's wrapper text, and task titles use the person's words, not that wrapper.

## 0.13.1

Fixes from running bw fresh on its own repo (a real agent review steered only by bw's hooks).

- Security: the watch room escapes every text that comes from commits, prompts or agents (a commit subject with HTML
  showed as script on a page that holds the token for decisions and plan approvals). The Bash auto-allow for
  `plan wait|reply|open` trusts only the system Python (`python3` on PATH or its absolute path), not any file named python3.
- Map draft: code under a `scripts/` folder (or `ci/`, `gradle/`) gets code regions when no other code exists, so a repo
  whose code all lives in `plugin/scripts/` is no longer mapped as tests, docs and build only. `.bw/` is never mapped as code.
- First probes: until a repo has a probe, any task that changes code (not tests, docs or build files) is asked once for its
  first 1 to 3 probes, whatever the map class. `bw on` says so.
- Hollow check aims its planted breaks at the functions a probe calls (names found in its script, plus what those call), or at
  the optional `"functions": ["name", ...]` the probe lists, instead of random lines anywhere in the covered files.
- `/bw:stats` counts review and `/bw:how` tasks as runs.
- Hosted sessions: a prompt that is only machine text (a "computer linked" status note) no longer opens or titles a task.
  With `BW_REPO`, bw logs only commands run in the repo or naming it, and never edits to files outside it.
- `dogfood/fresh-run.sh`: install bw from this folder into a clean copy of a repo with isolated Claude settings and no
  project memory, then run one real agent task, to see bw as a new user would.

## 0.13.0

- Review mode: a task whose first prompt asks to review or investigate and that changes no code is asked once for finding
  probes (`"finding"` field); `/bw:how` does the same on purpose and refuses edits outside `.bw/`. Findings go to the watch
  room (Fix this / Expected / Skip) and to `.git/bw/findings.json`. CLI: `how`, `findings [fix|expected|skip <id>]`.
- Brief on the next prompt (CHANGE / PRESERVE / AVOID / RISK) from findings, beliefs and decisions only. CLI: `brief`.
- Receipt rows carry a verdict (changed / same / can't tell / flaky) and an intent (asked: fixed or not fixed / kept /
  changed, not asked). A probe that newly fails to run is flagged, and "may be tied to code that moved" when every other
  probe is unchanged. Optional probe fields `entry` and `role: "safety"`.
- Beliefs: a probe that cannot run (timeout, command not found) is "can't tell", not broken; re-keeping appends to history;
  breaks that come back are recorded. `beliefs --history`.
- `/bw:why <probe>`, `/bw:recall [region|path]`, `hollow <probe>` (planted-break check, a warning only).
- `skills/bw-probes`: how to write probes (drive the public way in, a table of inputs, print what users observe).
- `BW_REPO=/path/to/repo` lets a session that runs outside the repo (a cloud session) still use bw on it; such sessions
  get review mode only through `/bw:how`.
- Ideas adapted from pstack (MIT, cursor/plugins): read-only investigation first, findings kept only when a run shows them,
  the Preserve/Change/Avoid/Risk brief. Reimplemented; no pstack code or text copied.

## 0.12.0
- Behavior receipts replace the agent-written cards. bw runs the repo's probes on the code before and after each task and shows both outputs; the catch-up item is "Behavior receipt" with Keep as expected / Not intended… / Leave open.
- When a costly region changes with no probe showing it, bw asks the agent for probes (not cards), takes the end snapshot again, and builds the receipt from them. A reply starting `NO PROBE:` is shown with the "No executed evidence" item.
- Beliefs: Keep stores the output, per person, with a copy of the probe (editing or deleting the probe in the repo cannot hide a break). Every later task re-runs them; a break shows as "Your belief broke" only to the person who kept it.
- Any agent: `task start` / `task end`, `receipt`, `keep`, `beliefs --check` and `agents-md` on the command line. New commands `/bw:receipt` and `/bw:beliefs`.
- The end-of-task line counts probes and beliefs: "receipt: 2 of 3 probes changed output; 1 belief re-run, 0 broke."
- Stop hook timeout raised to 300 s for probe runs. Older tasks keep their cards.

## 0.11.1
- `/bw:plan <file.md>` opens a plan you wrote as a Markdown file (not through plan mode) as a plan page. Claude waits for your answer the same way; after you approve, bw checks the code against that plan. When a task writes a file like `*PLAN*.md`, bw's end-of-task line suggests the command.
- A repo whose map has no regions gets one drafted from its folders on the next task. Before, bw saw nothing as costly there and stayed silent. `bwcap.py on --map FILE` now stops when FILE doesn't exist instead of saving an empty map.
- Test runs only count when the test runner starts a command. Words like `vitest` inside `npm view` or `mv` no longer count. A run followed by `; echo $?`, `|| true` or a pipe has an unknown result.
- Plan drift and decision checks compare file contents, so untracked files no longer show up as changed.

## 0.11.0
- Plan mode goes through bw. When Claude finishes a plan in plan mode, bw opens it as a page in your browser instead of the terminal dialog. Comment on any part, add a note, then Request changes or Approve plan. Claude waits in the background and wakes when you answer: a change request comes back as rev 2 (changed parts marked), an approval lets Claude start coding with your notes attached.
- The plan page checks every file the plan names against git: found, not in the repo yet, or a line past the end of the file.
- After you approve, bw compares what changed with the plan: at the end of each task it names changed files the plan never mentioned and planned files left untouched. The plan page shows the same under "Since you approved".
- Test runs are tied to the code they ran on. Items now say "Tests passed on this code", "Code changed after tests", "Tests failing" or "Test result unknown" (output piped into another command), instead of only "No test touched".
- Decisions are tied to the code you decided on. If that code changes outside an agent task (a hand edit, a merge), the catch-up shows "Changed outside an agent task". When an agent is about to edit it, bw tells the agent whether the code changed since your decision.
- `/bw:replies` prints your latest answer on a plan page, for when Claude was not waiting.
- Turn the plan page off with `BW_PLAN_GATE=0`. It also stays off in CI, in bypass-permissions mode, and in repos where bw is off.

## 0.10.0
- One catch-up per run of work: a prompt within 20 minutes of the last task in the same session ("do it", "fix the most important ones") continues the run. The run is measured from where it began, only its last task shows items, and the page opens once per run. A follow-up that leaves costly regions alone keeps the run's cards instead of asking again.
- "No test touched": items are marked when no test file changed and no test command ran in the run. It does not say whether existing tests cover the change.
- Cards are asked in the user's terms (what the user does and sees), with code only in the before/now steps.
- At most 5 open items show; the rest sit behind "Show more". An open page offers Refresh when newer work exists.
- `/bw:stats` shows local use counts from .git/bw/use.jsonl (runs, catch-ups, pages opened, answers, decisions). Nothing leaves your machine.

## 0.9.8
- `/bw:map [region] [cheap|fix|costly|critical]` shows or changes the map.
- `/bw:home` serves the watch room on 127.0.0.1, so your decisions are saved in .git/bw. A later task that edits those files tells the agent about them, and the watch room flags it.
- Desktop app: upload `bw-desktop.zip`.

## 0.9.8
- Steer from the watch room: Change it… → pick the outcome you want → "Do it now" (paste `/bw:do`) or "Record for next time".
- Before an agent edits code you decided about, bw stops that edit once and shows your decision; the agent then says if it changes it.
- Check-now questions: the agent's answer comes back as a card. Older open questions go quiet once a later task changed the same files.
- The page opens only when something needs you. Cache and .pyc files are ignored.
