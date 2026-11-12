# bw: a quick start for friends

Thanks for trying bw. It's a Claude Code plugin. When an agent changes your code, bw runs small "probe" commands on the
code from before the change and after it, and shows you both outputs side by side. If you mark an output as expected,
bw checks it again after every later task and tells you if it changes. It works in any git repo and any language.

## Install (about a minute)

You need Claude Code, python3 (3.8 or newer) and git, on macOS, Linux or WSL. Native Windows is not supported yet.

1. Install the plugin. The repo is private, so your git needs access to GitHub (an existing `gh auth login` or SSH key
   will do):

       claude plugin install bw --marketplace alincatalin/bwatch

2. Quit any open Claude Code sessions and start one in a git repo you work on.
3. Type `/bw:on`. It checks your setup and asks once about telemetry.
4. Type `/bw:onboard`. It takes 10 to 15 minutes and changes no code. bw scans the repo (structure, tests, docs such as
   AGENTS.md and README, and which areas change most), then the agent writes small probes for the most important
   behaviors in each area. bw runs every probe twice and plants breaks in the code to check each one would notice a
   change. You get an onboarding page with what it found.
5. Open the watch room link it prints and go through the probes: **Keep** if the output is what the code should do,
   **Wrong** if it isn't, **Skip** if you don't care. What you keep, bw re-checks after every later task.

bw does nothing in a repo until you run `/bw:on` there.

## What to expect

- **After onboarding**, each code task ends with a receipt: the probes run on the code before and after the change, and
  you see both outputs side by side. If something you kept changes, bw says so first.
- **The watch room** (`/bw:home`) is a local page on 127.0.0.1 showing receipts, findings and your decisions.
- **Something looks off?** Run `/bw:doctor`. It checks python, git and the hooks, and lists recent bw errors.

## What bw keeps, and where

- Per repo: `.git/bw/`. bw never commits anything. Probes you choose to keep live in `.bw/probes/`, and committing them
  is up to you.
- bw's own data: `~/.config/bw/` (counts, the error log and your telemetry choice).

## Telemetry: off unless you turn it on

Turning it on helps me see what works and what breaks. With `/bw:telemetry on`, bw sends the counts from each finished
day, at most once a day:

- how many tasks, reviews, receipts and probes ran, and how many changed, stayed the same, or couldn't run;
- decisions by kind (fix, expected, skip, keep), pages opened, which bw commands you used, and how often each hook ran;
- versions of bw, Claude Code, Python and git, and your OS family;
- bw's own errors as a fingerprint: which hook, which bw function and line, the Python error type, and a cause code.

It never sends paths, file or folder names, repo names, code, diffs, probe output, prompts, agent text, finding text, your
name, your email or your hostname. This is enforced in code: every field has to be on a fixed list, and any event that
doesn't fit is dropped whole. The ids it uses are random.

- `/bw:telemetry show` prints exactly what the next send would contain. `/bw:telemetry log` shows what was already sent.
- `/bw:telemetry off` stops it. `DO_NOT_TRACK=1` or `BW_TELEMETRY=0` also stop it, and it never sends from CI.
- To be clear: there are only a few of you, so I could probably guess whose counts are whose from when you installed.
  The data is pseudonymous rather than anonymous.
- Full error messages stay on your machine. If I ask about an error, `/bw:doctor e1a2` shows it with your paths replaced.
  Read it first, and send it only if you're happy to.

## Updating and removing

- Update: `claude plugin update bw@bw`, then restart Claude Code.
- Turn off in one repo: `/bw:off`. Remove completely: `claude plugin uninstall bw@bw`, then delete `.git/bw/` in your
  repos and `~/.config/bw/`.

## Feedback

Message me directly. The most useful things to tell me are when bw showed you something you didn't know, when it got in
your way, and when you ignored it.
