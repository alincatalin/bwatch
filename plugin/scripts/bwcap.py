#!/usr/bin/env python3
"""bw capture: shows each agent task as executed behavior, and tells you when something you rely on stops being true.

Claude Code hooks call this script:
  UserPromptSubmit -> `bwcap.py hook prompt`  : open a task, snapshot the working tree
  PostToolUse      -> `bwcap.py hook post`    : log the files the agent edits and the commands it runs
  Stop             -> `bwcap.py hook stop`    : snapshot again, run the repo's probes on both snapshots (the
                                                behavior receipt), re-run your beliefs, ask for probes if a
                                                costly region changed with none, render the watch room

Any agent, git hook or CI can use the same pieces from the command line:
  bwcap.py receipt [--from REF] [--to REF] [--json]   probes on two versions of the code (default HEAD -> working tree)
  bwcap.py keep <probe-id> [--at REF]                 record a probe's output as your belief
  bwcap.py beliefs [--check [REF]]                    list your beliefs, or re-run them (exit 1 if one broke)
  bwcap.py agents-md                                  instructions to paste into AGENTS.md / CLAUDE.md / rules files
  bwcap.py task start "<ask>" | task end              task boundaries for agents without bw hooks (exit 3 = add probes)
  bwcap.py how "<question>"                           read-only investigation: findings come back as probes
  bwcap.py findings [fix|expected|skip <probe>]       review findings, or decide one
  bwcap.py brief                                      what an agent is told before it edits (Change/Preserve/Avoid/Risk)
  bwcap.py why <probe-id>                             the commit and bw task where a probe's output became what it is
  bwcap.py recall [region|path]                       one area's state from bw's records
  bwcap.py hollow <probe-id>                          plant small breaks in the code a probe covers; warn if none shows

A probe is .bw/probes/<id>/probe.json {"scenario", "cmd", "covers"} plus any script beside it, in any language.
Everything bw shows comes from git snapshots and executed commands; no LLM writes what the engineer sees.
State lives in <git-common-dir>/bw/ and is never committed. Standard library only.
"""
import fnmatch, hashlib, html, json, os, random, re, shlex, shutil, subprocess, sys, time, uuid

BIG_UNTRACKED = 5 * 1024 * 1024
MAX_ASK_ROUNDS = 1
# A prompt that arrives this soon after the previous task in the same session continues the same run of work
# ("code review" -> "implement the fixes" -> "do it"): one catch-up covers the whole run.
RUN_GAP = 20 * 60
# Test evidence from git and the command log only. It says whether a test was touched, never whether it covers the change.
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|androidTest|androidUnitTest|commonTest|jvmTest|iosTest|unitTest|testDebug|testRelease)(/|$)"
                       r"|(_test|_spec|\.test|\.spec|Tests?|Spec)\.[A-Za-z]+$|(^|/)test_[^/]+\.py$", re.I)
# A test runner counts only where a command starts (after cd …&&, env vars, npx/uv run), never as a word inside
# another command ("npm view vitest", "mv vitest.config.ts …").
TEST_RUNNER = re.compile(r"^(?:\w+=\S*\s+)*(?:(?:npx|bunx|pnpx)\s+|(?:uv|poetry|pipenv|hatch)\s+run\s+|time\s+)?(?:"
                         r"(?:\S*/)?(?:pytest|jest|vitest|mocha|rspec|phpunit|tox|nox|ctest)\b"
                         r"|(?:go|cargo|swift|dotnet|deno|mix|flutter|dart)\s+test\b"
                         r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test"
                         r"|(?:\S*/)?gradlew?\b.*\b(?:\w*[Tt]est\w*|check)\b"
                         r"|xcodebuild\b.*\btest\b|mvn\b.*\b(?:test|verify)\b"
                         r"|python3?\s+-m\s+(?:unittest|pytest)\b|make\s+(?:test|check)\b|bundle\s+exec\s+rspec\b)")
CMD_SEP = re.compile(r"\s*(&&|\|\||;|\||\n|\(|\))\s*")
# Decisions on the page that make an executed output the engineer's belief.
KEEP_DECISIONS = ("Kept", "Expected", "Looks fixed")
# A first prompt that asks the agent to look, not to change: a review or an investigation. If such a task ends with no
# code changed, bw asks the agent to show each finding with a probe (review mode).
REVIEW_PROMPT = re.compile(r"\b(code[- ]?review|review|audit|investigat\w*|inspect|find (the |any )?(bugs?|issues?|problems?)|look for (bugs?|issues?|problems?)"
                           r"|what('s| is) (wrong|broken)|how does|how do(es)? we|why does|why is|explain|walk me through|is (it|this) (safe|correct|right))\b", re.I)


def test_in(cmd):
    """(ran a test runner, the command's exit code may not be the tests') for one shell command line. The exit code
    is the tests' only when nothing after them can replace it: piped output, `; echo $?` or `|| true` hide it."""
    parts = CMD_SEP.split(cmd or "")
    for i in range(0, len(parts), 2):
        if TEST_RUNNER.search(parts[i].strip()):
            after = parts[i + 1::2]
            return True, any(sep in ("|", ";", "\n", "||") for sep in after)
    return False, False


# ---------- git ----------

def git(args, cwd, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(["git"] + args, cwd=cwd, env=e, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def repo_root(cwd):
    try:
        return git(["rev-parse", "--show-toplevel"], cwd).strip()
    except Exception as e:
        # "not a git repository" is normal; anything else (e.g. git's "dubious ownership" refusal) silently turns bw
        # off, so leave a trace where hook errors go.
        if "not a git repository" not in str(e):
            tele_error("repo_root", e)
        return None


def git_common(root):
    common = git(["rev-parse", "--git-common-dir"], root).strip()
    return common if os.path.isabs(common) else os.path.join(root, common)


def enabled(root):
    """bw stays silent in a repo until `bwcap.py on` is run there."""
    try:
        return os.path.exists(os.path.join(git_common(root), "bw", "enabled"))
    except Exception:
        return False


def bw_dir(root):
    common = git(["rev-parse", "--git-common-dir"], root).strip()
    if not os.path.isabs(common):
        common = os.path.join(root, common)
    d = os.path.join(common, "bw")
    os.makedirs(os.path.join(d, "tasks"), exist_ok=True)
    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    return d


def snapshot(root, task_id, label):
    """Commit the whole working tree (tracked + small untracked files) without touching the real index."""
    bwd = bw_dir(root)
    idx = os.path.join(bwd, "index-%s" % label)
    env = {"GIT_INDEX_FILE": idx}
    head = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).strip()
    if head:
        git(["read-tree", head], root, env)
    else:
        git(["read-tree", "--empty"], root, env)
    git(["add", "-u"], root, env)
    untracked = [p for p in git(["ls-files", "-o", "--exclude-standard", "-z"], root).split("\0") if p]
    small = []
    for p in untracked:
        try:
            if os.path.getsize(os.path.join(root, p)) <= BIG_UNTRACKED:
                small.append(p)
        except OSError:
            pass
    for i in range(0, len(small), 200):
        git(["add", "--"] + small[i:i + 200], root, env)
    tree = git(["write-tree"], root, env).strip()
    args = ["commit-tree", tree, "-m", "bw snapshot %s %s" % (task_id, label)]
    if head:
        args[2:2] = ["-p", head]
    commit = git(args, root, {"GIT_AUTHOR_NAME": "bw", "GIT_AUTHOR_EMAIL": "bw@local",
                              "GIT_COMMITTER_NAME": "bw", "GIT_COMMITTER_EMAIL": "bw@local"}).strip()
    git(["update-ref", "refs/bw/%s/%s" % (task_id, label), commit], root)
    try:
        os.remove(idx)
    except OSError:
        pass
    return commit


def show(root, commit, path):
    r = subprocess.run(["git", "show", "%s:%s" % (commit, path)], cwd=root, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# ---------- state ----------

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def use(bwd, ev, **kw):
    """Local use counter: one line per event in .git/bw/use.jsonl. Never leaves the machine."""
    try:
        with open(os.path.join(bwd, "use.jsonl"), "a") as f:
            f.write(json.dumps(dict(ev=ev, at=round(time.time()), **kw)) + "\n")
    except Exception:
        pass
    try:
        tele_use(bwd, ev, kw)
    except Exception:
        pass


def session_file(bwd, sid):
    return os.path.join(bwd, "sessions", re.sub(r"[^A-Za-z0-9_-]", "_", sid or "none") + ".json")


def task_path(bwd, tid):
    d = os.path.join(bwd, "tasks", tid)
    os.makedirs(d, exist_ok=True)
    return d


def open_task(bwd, sid):
    s = load(session_file(bwd, sid), {})
    tid = s.get("open")
    if not tid:
        return None, None
    d = task_path(bwd, tid)
    return d, load(os.path.join(d, "task.json"), None)


# ---------- map ----------

def glob_match(pattern, path):
    # '**' spans directories, '*' stays inside one segment
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            rx += ".*"; i += 2
        elif pattern[i] == "*":
            rx += "[^/]*"; i += 1
        elif pattern[i] == "?":
            rx += "[^/]"; i += 1
        else:
            rx += re.escape(pattern[i]); i += 1
    return re.fullmatch(rx, path) is not None


def load_map(root, bwd):
    for p in (os.path.join(root, ".bw", "map.json"), os.path.join(bwd, "map.json")):
        m = load(p, None)
        if m and m.get("regions"):
            return m
    # No map, or one with no regions (an earlier `on --map` pointed at a file that did not exist): with no regions bw
    # never sees anything costly and stays silent, so draft one from the folders and keep it.
    try:
        m = draft_map(root)
        save(os.path.join(bwd, "map.json"), m)
        return m
    except Exception:
        return {"regions": []}


def region_of(m, path):
    for r in m.get("regions", []):  # first match wins, so specific regions go first in the file
        if any(glob_match(g, path) for g in r.get("paths", [])):
            return r["name"]
    return None


# ---------- transcript ----------

def last_agent_text(transcript_path, since_ts):
    """The agent's last plain-text message: its own account of the task."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last = ""
    with open(transcript_path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "assistant" or e.get("isSidechain"):
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if any(t.strip() for t in texts):
                    last = "\n".join(texts)
            elif isinstance(content, str) and content.strip():
                last = content
    return last.strip()


# ---------- hooks ----------

def hook_root(inp):
    """The repo a hook works on: the session's folder, or BW_REPO when the session runs outside any repo where bw is on
    (a cloud session that edits a repo by absolute path). Work found through BW_REPO never turns on review mode by itself:
    such a session also does work unrelated to that repo."""
    root = repo_root(inp.get("cwd") or os.getcwd())
    if root and enabled(root):
        return root
    env = os.environ.get("BW_REPO", "").strip()
    if env and os.path.isdir(env):
        r = repo_root(env)
        if r and enabled(r):
            inp["_via_env"] = 1
            return r
    return root

CHAT_DECISION = re.compile(r"^\s*(fix|expected|skip)\s+finding\s+([\w.-]+)\s*\.?\s*$", re.I)
WRAPPED = re.compile(r"^\s*<(wake|coordinator-relay|system-note|system-reminder)\b", re.S)


def clean_prompt(raw):
    """Hosted sessions wrap the person's words in machine text (wake envelopes, relays, notes). Keep only the words a
    person or the coordinator wrote, so task titles and review detection read the real ask."""
    s = (raw or "").strip()
    if not WRAPPED.match(s):
        return s
    for pat in (r"<message\b[^>]*\bfrom=\"human\"[^>]*>(.*?)</message>",
                r"<cited\b[^>]*\bauthor=\"user\"[^>]*>(.*?)</cited>",
                r"<note>(.*?)</note>",
                r"<message\b[^>]*>(.*?)</message>"):
        found = [m.strip() for m in re.findall(pat, s, re.S) if m.strip()]
        if found:
            return html.unescape("\n".join(found))
    # Nothing a person wrote (e.g. a "computer linked" status wake): no words to keep. The caller skips such prompts.
    s = re.sub(r"<(system-note|system-reminder)\b.*?</\1>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def hook_prompt(inp):
    root = hook_root(inp)
    if not root or not enabled(root):
        return
    bwd = bw_dir(root)
    sid = inp.get("session_id", "none")
    d, task = open_task(bwd, sid)
    raw = inp.get("prompt") or ""
    prompt = clean_prompt(raw)
    if raw.strip() and not prompt:
        return  # machine text only (a status wake): neither a new task nor a follow-up
    # Background-agent completions arrive as prompts; they belong to the task already running, never start one.
    if prompt.startswith("<task-notification>"):
        if task and not task.get("ended"):
            task.setdefault("notifications", 0)
            task["notifications"] += 1
            save(os.path.join(d, "task.json"), task)
        return
    cm = CHAT_DECISION.match(prompt)
    if cm:
        f = decide_finding(root, bwd, cm.group(1).lower(), cm.group(2), mode="chat")
        if f:
            note = "bw recorded the engineer's decision on review finding %r (probe %s): %s." % (f["finding"], f["pid"], f["status"])
            if f["status"] != "fix":
                return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note + " Nothing to change; confirm in one line."}}
    if task and not task.get("ended"):
        task.setdefault("prompts", []).append(prompt)  # a follow-up inside the same task
        save(os.path.join(d, "task.json"), task)
        return
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    d = task_path(bwd, tid)
    start = snapshot(root, tid, "start")
    task = {"id": tid, "session": sid, "root": root, "prompts": [prompt], "started": time.time(),
            "start": start, "own_start": start, "edits": [], "commands": [], "transcript": inp.get("transcript_path"), "asks": 0}
    sess = load(session_file(bwd, sid), {})
    last = sess.get("last") or {}
    if last.get("tid") and time.time() - last.get("ended", 0) < RUN_GAP and not prompt.startswith("/bw:"):
        prev = load(os.path.join(bwd, "tasks", last["tid"], "task.json"), None)
        if prev and prev.get("start"):
            # Same run of work: measure from where the run began, and keep what the agent did so far.
            task.update({"run": prev.get("run") or prev["id"], "prev": prev["id"], "start": prev["start"],
                         "run_title": prev.get("run_title") or task_title(prev), "run_n": prev.get("run_n", 1) + 1,
                         "edits": list(prev.get("edits", [])), "commands": list(prev.get("commands", [])),
                         "tests_run": list(prev.get("tests_run", []))})
    if prompt.startswith("/bw:onboard"):
        task["mode"] = "onboard"  # read-only like /bw:how: the agent writes probes and .bw/project.json, nothing else
    elif prompt.startswith("/bw:how"):
        task["mode"] = "how"  # read-only on purpose: edits outside .bw/ are refused, findings come back as probes
    elif REVIEW_PROMPT.search(task_title(task)) and not prompt.startswith("/bw:") and not inp.get("_via_env"):
        task["review"] = 1  # only matters if the task ends with no code changed
    brief = None
    if not prompt.startswith("/bw:") or prompt.startswith("/bw:do"):
        try:
            brief = make_brief(root, bwd)
        except Exception:
            brief = None
    if brief:
        task["brief"] = brief
        tele_count(bwd, {"brief_sent": 1})
    save(os.path.join(d, "task.json"), task)
    sess["open"] = tid
    save(session_file(bwd, sid), sess)
    if brief:
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": brief["text"]}}



def hook_pre(inp):
    """Before the agent edits a file the engineer decided about: deny that edit once, with the decision,
    so the agent reads it before changing the code. The retry goes through."""
    root = hook_root(inp)
    if not root or not enabled(root):
        return None
    bwd = bw_dir(root)
    d, task = open_task(bwd, inp.get("session_id"))
    ti = inp.get("tool_input") or {}
    p = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not task or not p:
        return None
    rel = os.path.relpath(p, root) if os.path.isabs(p) else p
    if task.get("mode") in ("how", "onboard") and not rel.replace(os.sep, "/").startswith(".bw/"):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason":
                "bw: /bw:how is read-only. Do not change code in this task. Write only probes under .bw/probes/ that show what "
                "the code does now, then answer. The engineer decides on the findings first."}}
    hit = decisions_for_files(bwd, task["id"], [rel])
    if not hit:
        return None
    msg = ("bw: before you edit %s, read the engineer's earlier decisions about this code:\n%s\n"
           "If your edit keeps these, retry it now (it will go through). If it would change a kept behavior, or does what the "
           "engineer asked for, retry it and say so plainly in your final reply." % (rel, "\n".join(
               decision_line(x) + fresh_note(root, x) for x in hit)))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": msg}}


def note_test_run(root, task, cmd, ok):
    """A test command ran: keep whether it passed and a git snapshot of the code it ran on, so bw can later say
    whether the code changed after it. A test command piped into another command hides its exit code: unknown."""
    found, piped = test_in(cmd)
    if not found:
        return
    if ok and piped:
        ok = None
    try:
        pin = snapshot(root, task["id"], "test-%d" % len(task.get("tests_run", [])))
    except Exception:
        pin = None
    task.setdefault("tests_run", []).append({"cmd": cmd[:200], "t": time.time(), "ok": ok, "pin": pin})


def hook_postfail(inp):
    """PostToolUseFailure: a Bash command exited non-zero. Logged with its exit code; a failed test run is kept."""
    root = hook_root(inp)
    if not root or not enabled(root) or inp.get("tool_name") != "Bash":
        return
    bwd = bw_dir(root)
    d, task = open_task(bwd, inp.get("session_id"))
    if not task:
        return
    cmd = (inp.get("tool_input") or {}).get("command", "")
    if inp.get("_via_env") and not about_repo(root, cmd, inp.get("cwd")):
        return
    code = re.search(r"Exit code (\d+)", str(inp.get("error") or ""))
    task["commands"].append({"cmd": cmd[:500], "t": time.time(), "exit_code": int(code.group(1)) if code else None,
                             "interrupted": bool(inp.get("is_interrupt"))})
    if not inp.get("is_interrupt"):
        note_test_run(root, task, cmd, False)
    save(os.path.join(d, "task.json"), task)


def test_state(root, task):
    """What the run's tests say about the code as it ended: passed / failed / stale (code changed after the last run) /
    unknown (output piped) / none (no test command ran). From the command log and git only."""
    runs = task.get("tests_run") or []
    if not runs:
        return {"st": "none", "text": "No test command ran in this run of work."}
    last = runs[-1]
    when = time.strftime("%H:%M", time.localtime(last["t"]))
    if last["ok"] is False:
        return {"st": "failed", "text": "The last test run failed (%s, %s) and nothing ran after it." % (last["cmd"][:80], when)}
    if last["ok"] is None:
        return {"st": "unknown", "text": "The last test run's result is unknown: its exit code was piped or replaced by a later command (%s)." % last["cmd"][:80]}
    if not last.get("pin") or not task.get("end"):
        return {"st": "unknown", "text": "Tests passed at %s; bw could not compare the code since." % when}
    after = [f["path"] for f in changed_files(root, last["pin"], task["end"])]
    if after:
        return {"st": "stale", "files": after[:8], "text": "Tests passed at %s, then %d file%s changed: %s%s." % (
            when, len(after), "" if len(after) == 1 else "s", ", ".join(after[:4]), " …" if len(after) > 4 else "")}
    return {"st": "passed", "text": "Tests passed on the final code (%s, %s)." % (last["cmd"][:80], when)}


def about_repo(root, cmd, cwd=None):
    """Does a shell command work on this repo: run from inside it, or naming it by path (or through BW_REPO)?"""
    names = {root, os.path.realpath(root), os.environ.get("BW_REPO", "").strip().rstrip("/")} - {""}
    if cwd and any(os.path.realpath(cwd) == os.path.realpath(n) or os.path.realpath(cwd).startswith(os.path.realpath(n) + os.sep) for n in names):
        return True
    return any(n in (cmd or "") for n in names) or "$BW_REPO" in (cmd or "") or "${BW_REPO}" in (cmd or "")


def hook_post(inp):
    root = hook_root(inp)
    if not root or not enabled(root):
        return
    bwd = bw_dir(root)
    d, task = open_task(bwd, inp.get("session_id"))
    if not task:
        return
    tool = inp.get("tool_name", "")
    ti = inp.get("tool_input") or {}
    tr = inp.get("tool_response")
    if tool == "Bash" and inp.get("_via_env") and not about_repo(root, ti.get("command", ""), inp.get("cwd")):
        return  # a session outside the repo (BW_REPO) also does unrelated work: log only commands aimed at this repo
    if tool == "Bash":
        entry = {"cmd": ti.get("command", "")[:500], "t": time.time()}
        note_test_run(root, task, ti.get("command", ""), True)
        pw = re.search(r"bwcap\.py['\"]?\s+plan\s+wait\s+([a-z0-9-]+)", ti.get("command", ""))
        if pw and plan_meta(bwd, pw.group(1)):
            sess = load(session_file(bwd, inp.get("session_id")), {})
            sess["plan_active"] = pw.group(1)
            save(session_file(bwd, inp.get("session_id")), sess)
        if isinstance(tr, dict):
            for k in ("exit_code", "exitCode", "interrupted", "is_error"):
                if k in tr:
                    entry[k] = tr[k]
            err = (tr.get("stderr") or "")[-200:]
            if err:
                entry["stderr_tail"] = err
        task["commands"].append(entry)
    else:
        p = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        if p:
            if os.path.isabs(p):
                rp = os.path.realpath(p)
                if not (rp.startswith(os.path.realpath(root) + os.sep) or p.startswith(root + os.sep)):
                    return  # a file outside this repo
                p = os.path.relpath(rp, os.path.realpath(root))
            if not p.startswith(".git/"):
                task["edits"].append({"tool": tool, "file": p, "t": time.time()})
    save(os.path.join(d, "task.json"), task)



JUNK = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|node_modules|\.DS_Store)(/|$)|\.py[co]$|^\.bw/")


def changed_files(root, a, b):
    out = git(["diff", "--numstat", "-M", a, b], root)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], parts[2]
        if " => " in path:
            path = re.sub(r"\{([^}]*) => ([^}]*)\}", r"\2", path)
            path = path.split(" => ")[-1]
        if JUNK.search(path):
            continue
        files.append({"path": path, "add": int(add) if add.isdigit() else 0, "del": int(dele) if dele.isdigit() else 0})
    return files


CARD_SCHEMA = """{
  "cards": [{
    "title": "4-6 words naming the situation, never the outcome (it is shown before the question), e.g. Signing in on a new phone",
    "regions": ["region names from the list below"],
    "question": "what a user does or sees, in their words (screens, taps, messages, money, saved data), ending in: what happens now? No class, function, state or variable names",
    "options": ["2-4 short outcomes the user or the engineer would observe, no code names; exactly one matches the code now"],
    "answer": 0,
    "shared": ["steps that are the same before and after, max 3, max 8 words each"],
    "before": [{"text": "max 8 words", "at": "path:line or path:start-end", "quote": "verbatim code from that line at the START snapshot", "kind": "bad|good|warn|plain"}],
    "now":    [{"text": "max 8 words", "at": "path:line", "quote": "verbatim code from that line in the current files", "kind": "bad|good|warn|plain"}],
    "split": 0
  }],
  "unknown": ["things you did not check that would change the answer, max 3"]
}"""


def ask_reason(task, cards_path, regions_hit, files, region_names, acts=(), prior=None):
    lines = [
        ("bw debrief: this task changed regions the engineer marked as costly to put right (%s)." % ", ".join(regions_hit)) if regions_hit else
        "bw debrief: this task carried out requests the engineer made in the watch room.",
        "Before finishing, reply with ONE ```json block holding the cards below. Do not write a file and do not change code. Shape:",
        CARD_SCHEMA,
        "Rules: one card per user-visible behavior that changed, at most 3, most consequential first. "
        "Write the question and options for someone who has not read the code: describe what the user does and what they see, "
        "never internal states or names like Merging(1) or BackendMerged; the code belongs in the before/now steps. "
        "Before-steps cite the code at the start snapshot: read it with `git show %s:<path>`. "
        "Now-steps cite the current files. Quotes must be copied exactly (one line, max 80 characters); bw checks every one. "
        "'split' is the index of the first now-step that differs from before. "
        "Include a card for any behavior change the engineer did not ask for. Reply with the json block only, then stop." % task["start"],
        "Changed files: " + ", ".join(f["path"] for f in files[:40]) + (" ..." if len(files) > 40 else ""),
        "Region names: " + ", ".join(region_names),
    ]
    if task.get("prev"):
        lines.insert(1, "This prompt continues a run of work that began with: \"%s\". The cards must cover the whole run, from the start snapshot to now%s." % (
            task.get("run_title", ""), (". Earlier cards in this run: " + "; ".join(c.get("title", "") for c in prior) + ". Keep one if its behavior is unchanged, update or replace the rest") if prior else ""))
    if acts:
        lines.insert(2, "The engineer asked for: " + " | ".join(a["want"] for a in acts) + ". Include one card per request whose now-steps show the behavior as it is now, so the engineer can check it was done.")
    return "\n".join(lines)


def parse_at(at):
    m = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", (at or "").strip())
    if not m:
        return None, None, None
    a = int(m.group(2))
    return m.group(1), a, int(m.group(3) or a)


def verify_step(root, commit, step):
    path, a, b = parse_at(step.get("at"))
    quote = (step.get("quote") or "").strip()
    if not path or not quote:
        return "unchecked"
    text = show(root, commit, path)
    if text is None:
        return "missing"
    lines = text.splitlines()
    lo, hi = max(0, a - 3), min(len(lines), b + 2)  # allow 2 lines of drift either side
    window = "\n".join(lines[lo:hi])
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return "ok" if norm(quote) in norm(window) else "mismatch"


def task_title(task):
    """The first prompt as a readable one-line title: pasted blocks and tags dropped."""
    for p in task.get("prompts", []):
        t = re.sub(r"<pasted_content[^>]*>.*?(</pasted_content>|$)", " ", p or "", flags=re.S)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t[:140]
    return "Agent task (pasted text)"


def build_data(task, m, files, cards, account):
    root = task["root"]
    by_region = {}
    unmapped = []
    for f in files:
        r = region_of(m, f["path"])
        if r is None:
            unmapped.append(f)
            continue
        g = by_region.setdefault(r, {"files": 0, "add": 0, "del": 0, "paths": []})
        g["files"] += 1; g["add"] += f["add"]; g["del"] += f["del"]; g["paths"].append(f["path"])
    cited = set()
    for c in cards.get("cards", []):
        for side, commit in (("before", task["start"]), ("now", task["end"])):
            for s in c.get(side, []):
                s["check"] = verify_step(root, commit, s)
                p, _, _ = parse_at(s.get("at"))
                if p:
                    cited.add(p)
    loose = {}
    for f in files:
        if f["path"] not in cited:
            r = region_of(m, f["path"]) or "Not on the map"
            loose.setdefault(r, []).append(f["path"])
    agent_files = sorted({e["file"] for e in task.get("edits", [])})
    changed = {f["path"] for f in files}
    title = task.get("run_title") or task_title(task)
    if task.get("run_n", 1) > 1:
        title += " (+%d follow-up%s)" % (task["run_n"] - 1, "" if task["run_n"] == 2 else "s")
    return {
        "title": title,
        "tests": {"files": [f["path"] for f in files if TEST_PATH.search(f["path"])][:10],
                  "ran": [c["cmd"][:160] for c in task.get("commands", []) if test_in(c.get("cmd", ""))[0]][-5:]},
        "task": task["id"], "start": task["start"][:8], "end": task["end"][:8],
        "totals": {"files": len(files), "add": sum(f["add"] for f in files), "del": sum(f["del"] for f in files)},
        "regions": [{"name": r["name"], "class": r.get("class", "repair"), "layer": r.get("layer", "code"),
                     **by_region.get(r["name"], {"files": 0, "add": 0, "del": 0, "paths": []})} for r in m.get("regions", [])],
        "unmapped": [f["path"] for f in unmapped],
        "layers": m.get("layers", []),
        "cards": cards.get("cards", []),
        "unknown": cards.get("unknown", []),
        "loose": [{"region": k, "files": v} for k, v in loose.items()],
        "not_by_agent": sorted(changed - set(agent_files))[:20],
        "commands": [c["cmd"] for c in task.get("commands", [])][-12:],
        "account": account[:1500],
        "testrun": test_state(root, task) if task.get("end") else None,
    }


def render(data, out_path):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "debrief_template.html")) as f:
        tpl = f.read()
    blob = json.dumps(data).replace("</", "<\\/")
    with open(out_path, "w") as f:
        f.write(tpl.replace("/*BW_DATA*/null", blob))
    return out_path


def summarize(task, data):
    return {"id": task["id"], "title": data["title"], "ended": task["ended"],
            "regions": {r["name"]: r["files"] for r in data["regions"] if r["files"]},
            "files": data["totals"]["files"], "add": data["totals"]["add"], "del": data["totals"]["del"],
            "cards": [c.get("title", "") for c in data["cards"]], "unknown": len(data["unknown"]),
            "href": "tasks/%s/debrief.html" % task["id"], "run": task.get("run") or task["id"]}


BULK_FILES = 150


def _cls_rank(c):
    return {"answer": 0, "migrate": 1, "repair": 2, "undo": 3}.get(c, 2)


def best_cls(t, rid, cls_of):
    inv = {v: k for k, v in rid.items()}
    return min((cls_of.get(inv[x], "repair") for x in t["r"]), key=_cls_rank, default="repair")


def watch_data(bwd, m, root):
    """Everything the watch room shows, from bw's own records: the map, every task and backfilled commit,
    the behavior cards with their git checks, and what needs a person. No LLM, nothing invented."""
    regs = m.get("regions", [])
    rid = {r["name"]: "r%d" % i for i, r in enumerate(regs)}
    cls_of = {r["name"]: r.get("class", "repair") for r in regs}
    regions = [{"id": rid[r["name"]], "name": r["name"], "cls": r.get("class", "repair"), "layer": r.get("layer", "code"),
                "paths": r.get("paths", []), "uses": [rid[u] for u in r.get("uses", []) if u in rid]} for r in regs]
    layers = list(m.get("layers", []))
    for r in regions:
        if r["layer"] not in layers:
            layers.append(r["layer"])
    tdir = os.path.join(bwd, "tasks")
    tasks, behaviors, items, auto, files = [], {}, [], [], {}
    latest_in_run = {}
    for tid in sorted(os.listdir(tdir)):
        sm = load(os.path.join(tdir, tid, "summary.json"), None)
        if sm:
            run = sm.get("run") or tid
            if run not in latest_in_run or sm["ended"] >= latest_in_run[run][0]:
                latest_in_run[run] = (sm["ended"], tid)
    superseded = {tid for tid in os.listdir(tdir) if (load(os.path.join(tdir, tid, "summary.json"), {}) or {}).get("run")
                  and latest_in_run[load(os.path.join(tdir, tid, "summary.json"), {})["run"]][1] != tid}
    notest, testrun = {}, {}
    for tid in sorted(os.listdir(tdir)):
        sm = load(os.path.join(tdir, tid, "summary.json"), None)
        if not sm:
            continue
        data = load(os.path.join(tdir, tid, "data.json"), None)
        commit = tid.startswith("commit-")
        if data is None and not commit:
            tk = load(os.path.join(tdir, tid, "task.json"), None)
            if tk and tk.get("start") and tk.get("end"):
                try:
                    data = build_data(tk, m, changed_files(root, tk["start"], tk["end"]), load(os.path.join(tdir, tid, "cards.json"), {"cards": []}), tk.get("account") or "")
                    data["deleted"] = [l.split("\t", 1)[1] for l in git(["diff", "--name-status", "--no-renames", tk["start"], tk["end"]], root, check=False).splitlines() if l.startswith("D\t")]
                    save(os.path.join(tdir, tid, "data.json"), data)
                    sm["title"] = data["title"]
                except Exception:
                    data = None
        when = time.localtime(sm["ended"])
        t = {"id": tid, "label": tid[7:14] if commit else time.strftime("%H:%M", when), "kind": "commit" if commit else "task",
             "d": time.strftime("%Y-%m-%d", when), "ended": sm["ended"], "title": sm["title"], "files": sm["files"],
             "add": sm["add"], "del": sm["del"], "r": {rid[k]: v for k, v in sm.get("regions", {}).items() if k in rid},
             "href": sm.get("href") or ""}
        if t["files"] > BULK_FILES:
            t["bulk"] = 1
        tests = (data or {}).get("tests")
        if tests is not None and not commit:
            notest[tid] = not tests.get("files") and not tests.get("ran")
            t["tests"] = tests
        if (data or {}).get("testrun") and not commit:
            testrun[tid] = data["testrun"]
        if tid in superseded:
            t["inrun"] = 1
        if (data or {}).get("acts"):
            t["acts"] = [a["id"] for a in data["acts"]]
        for rg in (data or {}).get("regions", []):
            for pth in rg.get("paths", []):
                files.setdefault(pth, {"p": pth, "reg": rid.get(rg["name"]), "hist": [], "beh": []})["hist"].append(tid)
        cards = (data or {}).get("cards", [])
        t["beh"] = []
        for i, c in enumerate(cards):
            name = (c.get("title") or "Behavior").strip()
            bid = "b" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
            creg = [rid[x] for x in c.get("regions", []) if x in rid] or list(t["r"].keys())[:3]
            def lane(steps, split=None):
                out = []
                for j, st in enumerate(steps):
                    k = st.get("kind", "plain")
                    k = "" if k == "plain" else k
                    if split is not None and j == split:
                        k = (k + " diff").strip()
                    at = st.get("at", "")
                    chk = st.get("check")
                    if at and chk and chk != "ok":
                        at += " · quote not found (%s)" % chk
                    out.append({"t": st.get("text", ""), "at": at, "k": k})
                return out
            checks = [st.get("check") for side in ("before", "now") for st in c.get(side, [])]
            src = "git" if checks and all(x == "ok" for x in checks) else "unk"
            opts = c.get("options", [])
            ans = c.get("answer", 0) if isinstance(c.get("answer"), int) else 0
            now_lane = lane(c.get("now", []), c.get("split"))
            b = behaviors.setdefault(bid, {"id": bid, "name": name, "sit": c.get("question", ""), "regions": [], "versions": []})
            for x in creg:
                if x not in b["regions"]:
                    b["regions"].append(x)
            b["versions"].append({"task": tid, "sum": opts[ans] if ans < len(opts) else c.get("question", ""), "src": src, "lane": now_lane})
            t["beh"].append(bid)
            for st in c.get("now", []):
                pth = parse_at(st.get("at"))[0]
                if pth and pth in files and bid not in files[pth]["beh"]:
                    files[pth]["beh"].append(bid)
            cited = sorted({parse_at(st.get("at"))[0] for st in c.get("now", []) if parse_at(st.get("at"))[0]})
            best = min((cls_of.get(x, "repair") for x in c.get("regions", [])), key=_cls_rank, default="repair")
            # Agents put the right answer in the same slot every time (index 1 in all 6 real cards we checked), so shuffle,
            # with a fixed seed per card so the order is stable across renders.
            order_k = list(range(len(opts)))
            random.Random("%s-c%d" % (tid, i)).shuffle(order_k)
            items.append({"id": "%s-c%d" % (tid, i), "kind": "predict", "beh": bid, "task": tid, "cls": best, "title": html.escape(name),
                          "regions": creg, "q": c.get("question", ""), "opts": [[chr(97 + k), opts[k]] for k in order_k],
                          "ans": chr(97 + ans), "before": lane(c.get("before", []), c.get("split")), "now": now_lane,
                          "shared": c.get("shared", []), "src": src, "files": cited,
                          "revisit": ", ".join(os.path.basename(x) for x in cited[:3]) or "these files"})
        unk = (data or {}).get("unknown", [])
        tfiles = [x for rg in (data or {}).get("regions", []) for x in rg.get("paths", [])]
        tfiles = sorted(tfiles, key=lambda x: _cls_rank(cls_of.get(region_of(m, x) or "", "repair")))[:8]
        for ask in (data or {}).get("acts", []):
            items.append({"id": "%s-r-%s" % (tid, ask["id"]), "kind": "result", "beh": t["beh"][0] if t["beh"] else None, "task": tid,
                          "cls": "answer", "title": "Did the agent do what you asked?", "regions": list(t["r"].keys())[:3], "files": tfiles,
                          "want": ask["want"],
                          "text": html.escape("You asked: %s. This task changed %s.%s" % (ask["want"], ", ".join(tfiles[:4]) or "no files",
                                  " Its cards are below." if t["beh"] else "")) +
                                  ("<br><br><b>The agent's answer</b> (its own account, not checked by bw):<br>" + html.escape(re.sub(r"```json.*?```", "", (data or {}).get("account", ""), flags=re.S).strip()[:900]).replace("\n", "<br>")
                                   if ask["want"].startswith("Check") and (data or {}).get("account") else "")})
        if unk:
            items.append({"id": "%s-u" % tid, "kind": "unknown", "files": tfiles, "beh": t["beh"][0] if t["beh"] else None, "task": tid,
                          "cls": "migrate", "title": "What the agent did not check" if len(unk) > 1 else "Open question",
                          "regions": list(t["r"].keys())[:3], "text": " · ".join(html.escape(u) for u in unk[:3])})
        costly = [x for x in (data or {}).get("deleted", []) if cls_of.get(region_of(m, x) or "", "repair") in ("answer", "migrate")]
        if costly:
            items.append({"id": tid + "-del", "kind": "removed", "beh": None, "task": tid, "cls": "answer", "title": "Files removed in costly regions",
                          "regions": sorted({rid[region_of(m, x)] for x in costly}), "files": costly, "text": "Deleted: " + ", ".join(costly[:6]) + (" …" if len(costly) > 6 else ""),
                          "q": "Did you mean to remove these?"})
        rc = (data or {}).get("receipt") or {}
        rrows = [r for r in rc.get("rows", []) if "after" in r]
        same = [r for r in rrows if not r["changed"]]
        t_regs = list(t["r"].keys())[:3]
        for r in rrows:
            regs_r = sorted({rid[x] for x in (region_of(m, c) for c in r.get("covers", [])) if x in rid}) or t_regs
            cls_r = min((cls_of.get(x, "repair") for x in (region_of(m, c) for c in r.get("covers", [])) if x), key=_cls_rank, default=best_cls(t, rid, cls_of))
            if r.get("intent") == "asked" and not r.get("fixed"):
                # a finding the engineer picked to fix, whose probe still prints the same: the fix did not land
                items.append({"id": "%s-nf-%s" % (tid, r["probe"]), "kind": "notfixed", "task": tid, "beh": None, "pid": r["probe"],
                              "cls": "answer", "title": html.escape(r.get("finding") or r["scenario"]), "regions": regs_r,
                              "files": r.get("covers") or tfiles, "cmd": r.get("cmd", ""), "before": r["before"], "after": r["after"],
                              "q": r["scenario"], "verdict": r.get("verdict"), "why": r.get("why", "")})
                continue
            if not r["changed"]:
                continue
            items.append({"id": "%s-p-%s" % (tid, r["probe"]), "kind": "receipt", "task": tid, "beh": None, "pid": r["probe"],
                          "cls": cls_r,
                          "title": html.escape(r.get("finding") or r["scenario"]) if r.get("intent") == "asked" else html.escape(r["scenario"]),
                          "regions": regs_r, "files": r.get("covers") or tfiles,
                          "cmd": r.get("cmd", ""), "before": r["before"], "after": r["after"], "q": r["scenario"],
                          "same": [[x["probe"], x["scenario"]] for x in same][:8], "pin": rc.get("to"),
                          "intent": r.get("intent", ""), "crash": r.get("crash", 0), "pinned": r.get("pinned", 0), "role": r.get("role", "")})
        cant = [r for r in rrows if r.get("verdict") == "cant_tell"]
        if cant and not commit:
            items.append({"id": "%s-ct" % tid, "kind": "canttell", "task": tid, "beh": None, "cls": "repair",
                          "title": "%d probe%s could not run" % (len(cant), "" if len(cant) == 1 else "s"), "regions": t_regs, "files": tfiles,
                          "text": " · ".join(html.escape("%s: %s" % (r["scenario"][:80], r.get("why", "could not run"))) for r in cant[:4])})
        for r in (data or {}).get("findings") or []:
            regs_r = sorted({rid[x] for x in (region_of(m, c) for c in r.get("covers", [])) if x in rid}) or t_regs
            if r.get("verdict") != "shown":
                continue
            items.append({"id": "%s-f-%s" % (tid, r["probe"]), "kind": "finding", "task": tid, "beh": None, "pid": r["probe"],
                          "cls": min((cls_of.get(x, "repair") for x in (region_of(m, c) for c in r.get("covers", [])) if x), key=_cls_rank, default="repair"),
                          "title": html.escape(r["finding"]), "regions": regs_r, "files": r.get("covers") or [], "cmd": r.get("cmd", ""),
                          "before": "", "after": r["after"], "q": r["scenario"], "entry": r.get("entry", ""), "pin": (data or {}).get("end"),
                          "seed": 1 if (data or {}).get("onboard") else 0})
        other = [r for r in (data or {}).get("findings") or [] if r.get("verdict") != "shown"]
        if (data or {}).get("unshown") or other:
            items.append({"id": "%s-us" % tid, "kind": "unshown", "task": tid, "beh": None, "cls": "repair",
                          "title": "Findings no run showed", "regions": t_regs, "files": [],
                          "list": [html.escape(x) for x in (data or {}).get("unshown", [])] +
                                  [html.escape("%s (probe %s: %s)" % (r["finding"], r["probe"], r.get("why") or "output differs between runs")) for r in other]})
        if (data or {}).get("review"):
            t["review"] = [len([r for r in (data or {}).get("findings") or [] if r.get("verdict") == "shown"]), len((data or {}).get("unshown") or []) + len(other)]
        if rrows:
            t["rc"] = [len(rrows), len(rrows) - len(same)]
        if (data or {}).get("noprobe"):
            items.append({"id": tid + "-np", "kind": "noreceipt", "task": tid, "beh": None, "cls": "migrate",
                          "title": "No executed evidence for this change" if (data or {}).get("seed") else "No executed evidence for a costly change",
                          "regions": t_regs, "files": tfiles,
                          "text": html.escape(("This task changed code and the repo has no probe yet. %s" % data["noprobe"]) if (data or {}).get("seed") else
                              "This task changed %s, marked costly to put right. %s" % (", ".join((data or {}).get("costly") or []) or "costly code", data["noprobe"])),
                          "q": ", ".join(tfiles[:4])})
        if t.get("bulk"):
            items.append({"id": tid + "-bulk", "kind": "bulk", "task": tid, "cls": "undo",
                          "title": "Bulk change: %d files, +%d −%d" % (t["files"], t["add"], t["del"]), "regions": list(t["r"].keys()),
                          "text": "Too big to trace as one change. bw shows only where it landed."})
        if not commit and not t["beh"] and not t.get("bulk") and not t.get("acts") and not (t.get("rc") and t["rc"][1]) and not (data or {}).get("noprobe") \
                and not (data or {}).get("review"):
            hit = [n for n, x in rid.items() if x in t["r"]]
            costly_hit = [n for n in hit if cls_of[n] in ("answer", "migrate")]
            if costly_hit:
                t["untraced"] = 1
            else:
                auto.append({"task": tid, "why": "%d file%s in %s. No region marked costly to put right." % (t["files"], "" if t["files"] == 1 else "s", ", ".join(hit) or "no mapped region")})
        if not t["beh"]:
            del t["beh"]
        tasks.append(t)
    # Inside a run of work only the last task speaks: its diff and cards cover the whole run.
    items = [i for i in items if not (i["task"] in superseded and i["kind"] in ("predict", "unknown", "removed", "bulk", "receipt", "noreceipt", "notfixed", "canttell"))]
    # Beliefs that broke: only for the person who holds them, on the task where bw first saw the break.
    me = person(root)
    tids = {t["id"] for t in tasks}
    for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"]:
        if b["person"] != me:
            continue
        breaks = list(b.get("past", []))  # breaks the person already accepted, so their decision stays on the page
        if b["state"] == "holds" and b.get("broken"):
            breaks.append(dict(b["broken"], expect=b["expect"], kept_at=b["at"]))
        for br in breaks:
            if br.get("task") not in tids:
                continue
            cov = b["probe"].get("covers") or []
            tt = next(t for t in tasks if t["id"] == br["task"])
            items.append({"id": "%s-bb-%s" % (b["id"], hashlib.sha1(br["now"].encode()).hexdigest()[:6]), "kind": "broken", "task": br["task"],
                          "beh": None, "cls": "answer", "bel": b["id"], "pid": b["probe"]["id"],
                          "title": html.escape(b["probe"]["scenario"]), "regions": sorted({rid[x] for x in (region_of(m, c) for c in cov) if x in rid}) or list(tt["r"].keys())[:3],
                          "files": cov, "cmd": b["probe"]["cmd"], "before": br["expect"], "after": br["now"], "q": b["probe"]["scenario"],
                          "kept": time.strftime("%b %d", time.localtime(br["kept_at"])), "pin": br["commit"], "crash": br.get("crash", 0)})
    gone = {(i["task"], i["pid"]) for i in items if i["kind"] == "broken"}
    items = [i for i in items if not (i["kind"] == "receipt" and (i["task"], i["pid"]) in gone)]  # the break says it better
    auto = [a for a in auto if a["task"] not in superseded]
    for b in behaviors.values():
        b["versions"] = [v for v in b["versions"] if v["task"] not in superseded]
    for k in [k for k, b in behaviors.items() if not b["versions"]]:
        del behaviors[k]
    for i in items:
        if notest.get(i["task"]) and i["kind"] in ("predict", "unknown", "removed", "receipt", "noreceipt"):
            i["notest"] = 1
        if testrun.get(i["task"]) and i["kind"] in ("predict", "unknown", "removed", "receipt", "noreceipt"):
            i["tst"] = testrun[i["task"]]
    dcs_fresh = {}
    for dcs in load(os.path.join(bwd, "decisions.json"), {"decisions": []})["decisions"]:
        if dcs["kind"] != "predict" or dcs["decision"] not in ("Kept", "Asked for a change") or (dcs.get("mode") == "now" and dcs.get("taken")):
            continue
        explained = set()
        fresh = decision_fresh(root, dcs)
        dcs_fresh[dcs["id"]] = fresh
        for t in tasks:
            if t["kind"] != "task" or t["ended"] <= dcs["at"] or dcs["id"] in t.get("acts", ()):
                continue
            touched = sorted({f for f, x in files.items() if t["id"] in x["hist"]} & set(dcs["files"]))
            explained.update(touched)
            if touched:
                items.append({"id": "%s-k-%s" % (t["id"], dcs["id"]), "kind": "kept", "beh": None, "task": t["id"], "cls": "answer",
                              "title": ("Touches a behavior you kept: " if dcs["decision"] == "Kept" else "Touches a behavior you asked to change: ") + html.escape(dcs["title"]),
                              "regions": list(t["r"].keys())[:3], "files": touched,
                              "text": html.escape("On %s you %s: %s → %s. This task changed %s." % (time.strftime("%b %d", time.localtime(dcs["at"])), "kept it" if dcs["decision"] == "Kept" else "asked for a change", dcs["question"], dcs["behavior"] or "?", ", ".join(touched)))})
        outside = [f for f in fresh.get("files", []) if f not in explained] if fresh.get("st") == "changed" else []
        if outside and dcs.get("task") in {t["id"] for t in tasks}:
            items.append({"id": "%s-x-%s" % (dcs["id"], hashlib.sha1(",".join(outside).encode()).hexdigest()[:8]), "kind": "kept", "beh": None,
                          "task": dcs["task"], "cls": "answer",
                          "title": "Changed outside an agent task: " + html.escape(dcs["title"]),
                          "regions": [rid[r] for r in {region_of(m, f) for f in outside} if r in rid][:3], "files": outside,
                          "text": html.escape("On %s you %s: %s → %s. Since then %s changed in git, and no agent task bw watched made that change." % (
                              time.strftime("%b %d", time.localtime(dcs["at"])), "kept it" if dcs["decision"] == "Kept" else "asked for a change",
                              dcs["question"], dcs["behavior"] or "?", ", ".join(outside[:4])))})
    # An open question from an older task goes quiet once a later agent task has changed the same files:
    # the later task's own debrief is the current picture. Decided ones stay as they are.
    state_done = load(os.path.join(bwd, "state.json"), {}).get("done", {})
    ended = {t["id"]: t["ended"] for t in tasks}
    later_files = {}
    for t in tasks:
        if t["kind"] == "task":
            later_files[t["id"]] = {f for f, x in files.items() if t["id"] in x["hist"]}
    keep = []
    for it in items:
        if it["kind"] == "unknown" and it["id"] not in state_done and it.get("files"):
            newer = [tid2 for tid2, fs in later_files.items() if ended[tid2] > ended.get(it["task"], 0) and fs & set(it["files"])]
            if newer:
                continue
        keep.append(it)
    items = keep
    tasks.sort(key=lambda t: t["ended"])
    order = {t["id"]: i for i, t in enumerate(tasks)}
    for b in behaviors.values():
        b["versions"].sort(key=lambda v: order.get(v["task"], 0))
    items.sort(key=lambda i: (_cls_rank(i["cls"]), 0 if i.get("notest") else 1, -order.get(i["task"], 0)))
    on = load(os.path.join(bwd, "on.json"), {}).get("at", 0)
    head = git(["rev-parse", "--short", "HEAD"], root, check=False).strip()
    state = load(os.path.join(bwd, "state.json"), {})
    return {"repo": os.path.basename(root), "head": head, "on": on, "state": {"done": state.get("done", {}), "picked": state.get("picked", {}), "look": state.get("look", 0), "want": state.get("want", {}), "taken": state.get("taken", {})}, "layers": layers, "regions": regions, "tasks": tasks,
            "behaviors": list(behaviors.values()), "items": items, "auto": auto,
            "files": sorted(files.values(), key=lambda f: f["p"])[:400]}


# Watch-room fields the page escapes itself, or that hold HTML bw already escaped. Everything else that reaches the page
# (task titles from commit messages and prompts, region and behavior names, steps, decisions) is text, escaped here, so
# a commit subject like <img onerror=...> shows as text instead of running with the page's token.
PAGE_RAW_TOP = {"items", "state", "repo", "head"}
PAGE_ITEM_ESC = {"revisit"}
PREDICT_ESC = {"opts", "before", "now", "shared"}  # older prose cards: the agent's text, shown as steps and options


def _esc_all(x):
    if isinstance(x, str):
        return html.escape(x, quote=True)
    if isinstance(x, list):
        return [_esc_all(v) for v in x]
    if isinstance(x, dict):
        return {k: _esc_all(v) for k, v in x.items()}
    return x


def page_safe(wd):
    out = {k: (v if k in PAGE_RAW_TOP else _esc_all(v)) for k, v in wd.items()}
    out["items"] = [{k: (_esc_all(v) if k in PAGE_ITEM_ESC or (it.get("kind") == "predict" and k in PREDICT_ESC) else v)
                     for k, v in it.items()} for it in wd.get("items", [])]
    return out


def render_home(bwd, m, root, with_data=False):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "watch_template.html")) as f:
        tpl = f.read()
    out = os.path.join(bwd, "home.html")
    wd = watch_data(bwd, m, root)
    with open(out, "w") as f:
        f.write(tpl.replace("/*BW_DATA*/null", json.dumps(page_safe(wd)).replace("</", "<\\/")))
    write_copy(root, out)
    return (out, wd) if with_data else out


def write_copy(root, page):
    """Hosted (cloud) sessions can't open the 127.0.0.1 watch room. When bw runs on a repo named by BW_REPO, or when
    BW_PAGE names a file, keep a static snapshot there after each render (default <repo>/watch.html, gitignored)."""
    dest = os.environ.get("BW_PAGE")
    env_repo = os.environ.get("BW_REPO")
    if not dest and env_repo:
        try:
            if os.path.realpath(env_repo) == os.path.realpath(root):
                dest = os.path.join(root, "watch.html")
        except Exception:
            dest = None
    if not dest:
        return None
    try:
        with open(page) as f:
            body = f.read().replace("var COPY=/*BW_COPY*/false;", "var COPY=true;", 1)
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, dest)
        return dest
    except Exception:
        return None


def cards_from_text(text):
    """The cards the agent replied with: the last ```json block (or bare object) that holds a "cards" list."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    if not blocks and text and text.strip().startswith("{"):
        blocks = [text.strip()]
    for b in reversed(blocks):
        try:
            d = json.loads(b)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("cards"), list):
            return d
    return None


# ---------- decisions: the watch room saves them in the repo, the agent sees them later ----------

def save_state(root, bwd, posted):
    """Merge what the watch room posted into .git/bw/state.json and rebuild decisions.json (what the agent sees)."""
    st = load(os.path.join(bwd, "state.json"), {"done": {}, "picked": {}, "look": 0, "at": {}})
    st.setdefault("want", {})
    st.setdefault("taken", {})
    now = time.time()
    for k, v in (posted.get("done") or {}).items():
        if st["done"].get(k) != v:
            st.setdefault("at", {})[k] = now
            use(bwd, "decision", item=k, decision=v, mode=((posted.get("want") or {}).get(k) or {}).get("mode", ""))
    for k, v in (posted.get("picked") or {}).items():
        if k not in st.get("picked", {}):
            use(bwd, "predicted", item=k)
    for k, v in (posted.get("want") or {}).items():
        if st["want"].get(k) != v:
            st.setdefault("at", {})[k] = now
            st["taken"].pop(k, None)  # a new request is a new job
    for k in list(st["done"]):
        if k not in (posted.get("done") or {}) and posted.get("full"):
            st["done"].pop(k)  # undone in the page
    if posted.get("full"):
        st["want"] = {}
    st["want"].update(posted.get("want") or {})
    st["done"].update(posted.get("done") or {})
    st["picked"].update(posted.get("picked") or {})
    st["look"] = max(st.get("look", 0), float(posted.get("look") or 0))
    save(os.path.join(bwd, "state.json"), st)
    update_findings_from_state(bwd, st)
    items = {i["id"]: i for i in watch_data(bwd, load_map(root, bwd), root)["items"]}
    out = []
    for iid, dec in st["done"].items():
        it = items.get(iid)
        if not it or it["kind"] == "bulk" or dec in ("Skimmed",):
            continue
        ans = dict(it.get("opts", [])).get(it.get("ans"), "") if it["kind"] == "predict" else it.get("after", "")
        w = st["want"].get(iid) or {}
        out.append({"id": iid, "decision": dec, "at": st.get("at", {}).get(iid, now), "kind": it["kind"], "title": it["title"],
                    "behavior": ans, "question": it.get("q", ""), "text": re.sub(r"<[^>]+>", "", html.unescape(it.get("text", ""))),
                    "files": it.get("files", []), "task": it["task"], "told": [],
                    "want": w.get("text", ""), "mode": w.get("mode", ""), "taken": st["taken"].get(iid, 0),
                    "now": [x for x in it.get("now", [])],
                    "pin": (load(os.path.join(bwd, "tasks", it["task"], "task.json"), {}) or {}).get("end"),
                    "probe": ("%s/%s" % (PROBE_ROOT, it["pid"])) if it.get("pid") else None})
    old = {d["id"]: d for d in load(os.path.join(bwd, "decisions.json"), {"decisions": []})["decisions"]}
    for d in out:
        if d["id"] in old and old[d["id"]]["decision"] == d["decision"]:
            d["told"] = old[d["id"]].get("told", [])
    save(os.path.join(bwd, "decisions.json"), {"decisions": out})
    sync_beliefs(root, bwd, st, items)
    return st


def sync_beliefs(root, bwd, st, items):
    """Keep on a receipt = a belief. "I asked for this" on a broken belief = the new outcome becomes the belief.
    Reopening the Keep drops the belief again."""
    done = st.get("done", {})
    bl = load(beliefs_path(bwd), {"beliefs": []})
    by_item = {b.get("item"): b for b in bl["beliefs"] if b.get("item") and b["state"] == "holds"}
    for b in bl["beliefs"]:
        if b.get("item") and b["state"] == "holds" and done.get(b["item"]) not in KEEP_DECISIONS and b["item"] in items:
            belief_history(b, "reopened in the watch room")
            b["state"] = "dropped"
    save(beliefs_path(bwd), bl)
    for iid, dec in done.items():
        it = items.get(iid)
        if not it:
            continue
        if it["kind"] in ("receipt", "finding") and dec in KEEP_DECISIONS and iid not in by_item:
            tk = load(os.path.join(bwd, "tasks", it["task"], "task.json"), {}) or {}
            p = {x["id"]: x for x in load_probes(root, tk.get("end") or it["pin"])}.get(it["pid"])
            if p:
                add_belief(root, bwd, p, it["after"], tk.get("end") or it["pin"], task=it["task"], item=iid,
                           why="kept again from " + ("a review finding" if it["kind"] == "finding" else "a receipt"))
        if it["kind"] == "broken" and dec == "I asked for this":
            bl = load(beliefs_path(bwd), {"beliefs": []})
            for b in bl["beliefs"]:
                if b["id"] == it["bel"] and b.get("broken"):
                    b.setdefault("past", []).append(dict(b["broken"], expect=b["expect"], kept_at=b["at"]))
                    belief_history(b, "I asked for this", task=b["broken"].get("task"))
                    b.update({"expect": b["broken"]["now"], "commit": b["broken"]["commit"], "at": time.time()})
                    b.pop("broken")
            save(beliefs_path(bwd), bl)


def decision_fresh(root, d):
    """Whether the code a decision is about still matches the code the engineer decided on: the decision is pinned to
    the end snapshot of the task it came from. as-decided / changed (with the files) / unknown."""
    pin, files = d.get("pin"), [f for f in d.get("files", []) if f]
    if not pin or not files:
        return {"st": "unknown"}
    if subprocess.run(["git", "cat-file", "-e", pin + "^{commit}"], cwd=root, capture_output=True).returncode:
        return {"st": "unknown"}
    changed = []
    for f in files:  # blob by blob, so an untracked file compares the same as a tracked one
        old = git(["rev-parse", "-q", "--verify", "%s:%s" % (pin, f)], root, check=False).strip() or None
        full = os.path.join(root, f)
        cur = git(["hash-object", "--", f], root, check=False).strip() if os.path.isfile(full) else None
        if old != cur:
            changed.append(f)
    return {"st": "changed", "files": changed} if changed else {"st": "as-decided"}


def fresh_note(root, d):
    f = decision_fresh(root, d)
    if f["st"] == "changed":
        return " (bw: this code has changed in git since that decision: %s)" % ", ".join(f["files"][:4])
    if f["st"] == "as-decided":
        return " (bw: the code is as it was when they decided)"
    return ""


def decision_line(d):
    when = time.strftime("%b %d", time.localtime(d["at"]))
    if d["kind"] == "predict":
        if d.get("want") and d["decision"] != "Kept":
            return "- %s: the engineer ASKED FOR A CHANGE (%s): %s. Situation: %s Current outcome: %s. Files: %s" % (
                d["title"], when, d["want"], d["question"], d["behavior"] or "not stated", ", ".join(d["files"][:4]))
        what = {"Kept": "KEPT this behavior; do not change it without saying so",
                "Asked for a change": "ASKED FOR A CHANGE to this behavior",
                "Left open": "left this open (undecided)"}.get(d["decision"], d["decision"])
        return "- %s: the engineer %s (%s). Situation: %s Outcome they confirmed: %s. Files: %s" % (
            d["title"], what, when, d["question"], d["behavior"] or "not stated", ", ".join(d["files"][:4]))
    if d["kind"] == "receipt" and d["decision"] == "Kept":
        return "- %s: the engineer KEPT this outcome (%s); do not change it without saying so. Probe %s printed, when they kept it: %s. Files: %s" % (
            d["title"], when, d.get("probe") or "?", " | ".join((d["behavior"] or "").splitlines()[:4])[:300], ", ".join(d["files"][:4]))
    if d.get("want"):
        return "- %s (%s): the engineer asked: %s. Files: %s" % (d["title"], when, d["want"], ", ".join(d["files"][:4]) or "see the task")
    return "- %s (%s): %s. %s" % (d["title"], when, d["decision"], d["text"])


def decisions_for_files(bwd, tid, paths):
    """Decisions about any of these files that this task has not been told yet; marks them told."""
    dp = os.path.join(bwd, "decisions.json")
    ds = load(dp, {"decisions": []})
    hit = [d for d in ds["decisions"] if d["decision"] not in ("Left open", "Looks right", "Accepted the change", "Meant to remove", "Skimmed",
                                                               "I asked for this", "Fine without")
           and not (d.get("mode") == "now" and d.get("taken")) and d["kind"] != "result"
           and tid not in d.get("told", []) and set(d.get("files", [])) & set(paths)]
    for d in hit:
        d.setdefault("told", []).append(tid)
    if hit:
        save(dp, ds)
    return hit


def server_info(bwd):
    info = load(os.path.join(bwd, "server.json"), None)
    if not info:
        return None
    try:
        os.kill(info["pid"], 0)
    except Exception:
        return None
    me = os.path.abspath(__file__)
    if info.get("script") != me or info.get("mtime") != os.path.getmtime(me):
        try:  # an older bw is serving; replace it
            os.kill(info["pid"], 15)
        except Exception:
            pass
        return None
    return info


def home_url(root, bwd):
    """http link to the watch room served from this machine (so your decisions reach the repo); file:// if that fails."""
    info = server_info(bwd)
    if not info:
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", root], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):
                time.sleep(0.05)
                info = server_info(bwd)
                if info:
                    break
        except Exception:
            info = None
    if info:
        return "http://127.0.0.1:%d/home.html?t=%s" % (info["port"], info["token"])
    return "file://" + os.path.join(bwd, "home.html")


def cmd_serve(args):
    """Serve .git/bw on 127.0.0.1 with a random port and token. Exits after 8 idle hours."""
    import http.server
    import urllib.parse
    root = repo_root(args[0] if args else os.getcwd())
    bwd = bw_dir(root)
    token = uuid.uuid4().hex
    last = [time.time()]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self, q):
            return q.get("t", [""])[0] == token or self.headers.get("X-BW-Token") == token

        def do_GET(self):
            last[0] = time.time()
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = urllib.parse.unquote(u.path).lstrip("/") or "home.html"
            ref = urllib.parse.parse_qs(urllib.parse.urlparse(self.headers.get("Referer") or "").query)
            if not (self._ok(q) or self._ok(ref)):
                self.send_error(403)
                return
            if path == "stamp":  # the open page asks whether newer agent work exists
                latest = 0
                for tid in os.listdir(os.path.join(bwd, "tasks")):
                    latest = max(latest, (load(os.path.join(bwd, "tasks", tid, "summary.json"), {}) or {}).get("ended", 0))
                body = json.dumps({"latest": latest}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "plan-stamp":  # the open plan page asks whether a newer revision or an answer exists
                pm = plan_meta(bwd, q.get("p", [""])[0]) or {}
                body = json.dumps({"rev": len(pm.get("revs", [])), "approved": (pm.get("approved") or {}).get("rev", 0),
                                   "passed": bool(pm.get("passed")), "replies": len(plan_replies(bwd, pm["id"])) if pm else 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            pm = re.match(r"^plans/([a-z0-9-]+)/plan\.html$", path)
            if pm:
                render_plan(bwd, root, pm.group(1))
                use(bwd, "plan-opened", plan=pm.group(1))
            if path == "home.html":
                render_home(bwd, load_map(root, bwd), root)
                use(bwd, "opened")
            full = os.path.realpath(os.path.join(bwd, path))
            if not full.startswith(os.path.realpath(bwd) + os.sep) or not full.endswith(".html") or not os.path.isfile(full):
                self.send_error(404)
                return
            body = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            last[0] = time.time()
            origin = self.headers.get("Origin")
            if self.headers.get("X-BW-Token") != token or (origin and origin != "http://127.0.0.1:%d" % port):
                self.send_error(403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                posted = json.loads(self.rfile.read(n) or b"{}")
                if urllib.parse.urlparse(self.path).path == "/plan-reply":
                    code, ans = plan_post(root, bwd, posted)
                    body = json.dumps(ans).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                save_state(root, bwd, posted)
                self.send_response(204)
                self.end_headers()
            except Exception:
                self.send_error(400)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    me = os.path.abspath(__file__)
    save(os.path.join(bwd, "server.json"), {"port": port, "token": token, "pid": os.getpid(), "script": me, "mtime": os.path.getmtime(me)})
    srv.timeout = 60
    while time.time() - last[0] < 8 * 3600:
        srv.handle_request()


def open_page(url):
    """Open a page in the default browser on macOS, Linux or Windows; quietly do nothing if none is found."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], capture_output=True, timeout=10)
        elif sys.platform.startswith("win"):
            os.startfile(url)
        elif shutil.which("xdg-open") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def hook_stop(inp):
    root = hook_root(inp)
    if not root or not enabled(root):
        return None
    bwd = bw_dir(root)
    sid = inp.get("session_id")
    d, task = open_task(bwd, sid)
    if not task:
        return None
    tp = os.path.join(d, "task.json")
    if not task.get("end") or ((task.get("probe_ask") or task.get("finding_ask")) and not task.get("reshot")):
        # second pass after a probe request: the probes the agent just wrote must be in the end snapshot
        if task.get("end"):
            task["reshot"] = 1
        task["end"] = snapshot(root, task["id"], "end")
        save(tp, task)
    files = changed_files(root, task["start"], task["end"])
    if task.get("mode") == "onboard":
        sess = load(session_file(bwd, sid), {})
        sess.pop("open", None)
        sess["last"] = {"tid": task["id"], "ended": time.time(), "run": task["id"]}
        save(session_file(bwd, sid), sess)
        task["ended"] = time.time()
        save(tp, task)
        res = load(os.path.join(onboard_dir(bwd), "result.json"), None)
        if not res or res.get("at", 0) < task["started"]:
            res = onboard_verify(root, bwd)
        url = home_url(root, bwd)
        page = url.replace("home.html", "onboard.html") if url.startswith("http") else "file://" + os.path.join(bwd, "onboard.html")
        if os.environ.get("BW_OPEN", "1") == "1":
            open_page(page)
        return {"systemMessage": "bw onboarding: %d probe%s run and stay stable, %d not used. See what bw knows and decide: %s" % (
            res["ok"], "" if res["ok"] == 1 else "s", res["failed"], page)}
    acting = load(os.path.join(bwd, "acting.json"), None)
    first = (task.get("prompts") or [""])[0].strip()
    if acting and first.startswith("/bw:do") and acting.get("at", 0) >= task["started"] - 60 and not task.get("acts"):
        task["acts"] = acting["acts"]
        save(tp, task)
        os.remove(os.path.join(bwd, "acting.json"))
    acts = task.get("acts", [])
    sess = load(session_file(bwd, sid), {})
    sess.pop("open", None)
    sess["last"] = {"tid": task["id"], "ended": time.time(), "run": task.get("run") or task["id"], "opened": (sess.get("last") or {}).get("opened")}
    if (task.get("mode") == "how" or task.get("review")) and not acts:
        own = files if not task.get("prev") else changed_files(root, task.get("own_start") or task["start"], task["end"])
        if not own:  # this prompt looked and changed nothing: review mode
            out = review_stop(root, bwd, task, d, inp, sess)
            if out is not None:
                return out
    if not files and not acts:
        save(session_file(bwd, sid), sess)
        return None
    m = load_map(root, bwd)
    classes = {r["name"]: r.get("class", "repair") for r in m.get("regions", [])}
    hit = sorted({region_of(m, f["path"]) for f in files} - {None})
    asking = [r for r in hit if classes.get(r) in ("migrate", "answer")]
    cards_path = os.path.join(d, "cards.json")
    prior_noprobe = None
    if task.get("prev"):
        own = changed_files(root, task.get("own_start") or task["start"], task["end"])
        own_costly = [r for r in sorted({region_of(m, f["path"]) for f in own} - {None}) if classes.get(r) in ("migrate", "answer")]
        if not own_costly:
            asking = []  # this follow-up left the costly regions alone: the run's receipt covers it, do not ask again
            prior_noprobe = (load(os.path.join(bwd, "tasks", task["prev"], "data.json"), {}) or {}).get("noprobe")
    last = inp.get("last_assistant_message") or last_agent_text(task.get("transcript") or inp.get("transcript_path"), task["started"])
    # Behavior receipt: the repo's probes run on the code at the run's start and at its end. Executed, not written.
    probes = load_probes(root, task["end"]) if files else []
    try:
        rc = receipt(root, bwd, task["start"], task["end"], probes) if probes else None
    except Exception as e:
        rc = {"rows": [], "error": repr(e)[:300], "error_id": tele_error("receipt", e, bwd)}
    if rc and rc.get("rows"):
        try:
            mark_intent(root, bwd, task, rc)
        except Exception as e:
            rc["intent_error"] = repr(e)[:300]
    shown = bool(rc and any(r.get("changed") for r in rc["rows"]))
    # First run: a repo with no probe at all gets nothing from receipts, whatever the map says. Until the first probe
    # exists, any change to code (not tests, docs or build files) gets one probe request, so value starts with the next task.
    seed = bool(files) and not probes and not asking and any(is_code(f["path"]) for f in files)
    if seed:
        asking = ["first probes for this repo"]
    want_probe = bool(asking or (acts and files))
    if want_probe and not shown and task.get("asks", 0) < MAX_ASK_ROUNDS and not inp.get("stop_hook_active") \
            and os.environ.get("BW_ASK_PROBES", "1") == "1":
        task["asks"] = task.get("asks", 0) + 1
        task["probe_ask"] = 1
        task["account"] = last  # the agent's own summary, before it answers bw's request
        save(tp, task)
        return {"decision": "block", "reason": probe_reason(task, asking, files, acts)}
    # Beliefs: every outcome this person kept, re-run on the code as the task left it.
    bel = None
    if files:
        try:
            broken, checked, skipped = check_beliefs(root, bwd, task["end"], [f["path"] for f in files], task["id"], who=person(root))
            bel = {"checked": checked, "broken": [b["id"] for b in broken], "skipped": skipped}
        except Exception as e:
            bel = {"checked": 0, "broken": [], "error": repr(e)[:300], "error_id": tele_error("beliefs", e, bwd)}
    cards = load(cards_path, {"cards": []})  # older tasks may hold prose cards; new tasks get receipts instead
    account = task.get("account") or last
    data = build_data(task, m, files, cards, account)
    data["deleted"] = [l.split("\t", 1)[1] for l in git(["diff", "--name-status", "--no-renames", task["start"], task["end"]], root, check=False).splitlines() if l.startswith("D\t")]
    data["acts"] = acts
    data["receipt"] = rc
    data["beliefs"] = bel
    if want_probe and not shown:
        data["noprobe"] = no_probe_reason(last) or prior_noprobe or ("The agent wrote no probe that shows a change." if task.get("probe_ask")
                                                                     else "No probe in .bw/probes/ shows this change.")
        data["costly"] = [] if seed else asking
        if seed:
            data["seed"] = 1
    save(os.path.join(d, "data.json"), data)
    out = render(data, os.path.join(d, "debrief.html"))
    task["ended"] = time.time()
    task["debrief"] = out
    save(tp, task)
    save(os.path.join(d, "summary.json"), summarize(task, data))
    _, wd = render_home(bwd, m, root, with_data=True)
    need = [i for i in wd["items"] if i["task"] == task["id"]]
    nt = [i for i in need if i.get("notest")]
    run = task.get("run") or task["id"]
    opened = sess["last"].get("opened") == run
    if need and not opened and os.environ.get("BW_OPEN", "1") == "1":
        sess["last"]["opened"] = run  # open the page once per run of work; later follow-ups only update it
    save(session_file(bwd, sid), sess)
    rows = [r for r in (rc or {}).get("rows", []) if "after" in r]
    nchg = sum(1 for r in rows if r["changed"])
    use(bwd, "stop", task=task["id"], run=run, items=len(need), notest=len(nt), files=len(files), follow=task.get("run_n", 1) > 1,
        probes=len(rows), changed=nchg, beliefs=(bel or {}).get("checked", 0), broke=len((bel or {}).get("broken", [])))
    tele_stop(bwd, task, files, rc, bel, data, last)
    proof = []
    if rows:
        proof.append("receipt: %d of %d probe%s changed output" % (nchg, len(rows), "" if len(rows) == 1 else "s"))
    if bel and bel.get("checked"):
        proof.append("%d belief%s re-run, %d broke" % (bel["checked"], "" if bel["checked"] == 1 else "s", len(bel["broken"])))
    proof = (" " + "; ".join(proof) + ".") if proof else ""
    if not need:  # nothing for a person: say so in one line, do not pull them out of the terminal
        return {"systemMessage": "bw: nothing needs you (%d file%s in %s).%s" % (len(files), "" if len(files) == 1 else "s", ", ".join(hit) or "unmapped code", proof)}
    url = home_url(root, bwd) + "#catchup"
    if sess["last"].get("opened") == run and not opened:
        open_page(url)
    pre = {"broken": "your belief broke: ", "receipt": "changed: ", "noreceipt": ""}
    titles = [pre.get(i["kind"], "") + html.unescape(re.sub(r"<[^>]+>", "", i["title"])) for i in need[:2]]
    return {"systemMessage": "bw: %d thing%s need%s you%s: %s%s.%s  %s" % (len(need), "" if len(need) == 1 else "s", "s" if len(need) == 1 else "",
            " (%d with no test touched)" % len(nt) if nt else "", "; ".join(titles), " …" if len(need) > 2 else "", proof, url)}



def hook_stop_all(inp):
    """The debrief, then one line on how the code drifted from an approved plan, if there is one."""
    out = hook_stop(inp)
    if out and out.get("decision") == "block":
        return out
    try:
        line = plan_drift_line(inp)
    except Exception:
        line = None
    try:
        hint = plan_file_hint(inp)
    except Exception:
        hint = None
    line = "\n".join(x for x in (line, hint) if x) or None
    if line:
        out = out or {}
        out["systemMessage"] = (out.get("systemMessage", "") + "\n" + line).strip()
    return out


# ---------- probes, receipts, beliefs ----------
# A probe is a tiny command, in any language, that runs one situation through the real code and prints what happens.
# Probes live in the repo under .bw/probes/ (written by any agent or by hand). bw runs each one on a clean copy of the
# code at two commits (the task's start and end) and shows the two executed outputs: a behavior receipt. When the
# engineer keeps an outcome it becomes their belief, re-run at every later task; only that person is told when it
# breaks. No LLM anywhere in this path: git, a shell and the repo's own toolchain.

PROBE_ROOT = ".bw/probes"
PROBE_TIMEOUT = int(os.environ.get("BW_PROBE_TIMEOUT", "30"))
PROBE_BUDGET = int(os.environ.get("BW_PROBE_BUDGET", "120"))
PROBE_MAX_FILE = 64 * 1024
# Installed dependencies are not in git; link them into each clean copy so probes can import them. Build outputs are
# never linked: they hold the current code and would make the "before" run show the "after" behavior.
DEP_DIRS = ["node_modules", ".venv", "venv", "vendor", "Pods", ".bundle", "deps", "_build"]
NORMALIZE = [
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "0xADDR"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<TIME>"),
    (re.compile(r"\b\d+(\.\d+)?\s?(ms|µs|us|ns|s)\b"), "<DUR>"),
    (re.compile(r"(/private)?/(tmp|var/folders)/[\w./-]+"), "<TMP>"),
]


def probe_id(s):
    return re.sub(r"[^a-z0-9_-]+", "-", (s or "").lower()).strip("-")[:60] or "probe"


def load_probes(root, commit):
    """Probes as they are at a commit: .bw/probes/<id>/probe.json (+ files beside it) or .bw/probes/<id>.json."""
    names = git(["ls-tree", "-r", "--name-only", commit, "--", PROBE_ROOT], root, check=False).splitlines()
    out = {}
    for n in names:
        rel = n[len(PROBE_ROOT) + 1:]
        parts = rel.split("/")
        if len(parts) == 1 and rel.endswith(".json"):
            pid, base = rel[:-5], None
        elif len(parts) == 2 and parts[1] == "probe.json":
            pid, base = parts[0], parts[0]
        else:
            continue
        try:
            spec = json.loads(show(root, commit, n) or "")
        except Exception:
            out[probe_id(pid)] = {"id": probe_id(pid), "bad": "probe.json is not valid JSON"}
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("cmd"), str) or not spec["cmd"].strip():
            out[probe_id(pid)] = {"id": probe_id(pid), "bad": "probe.json needs a \"cmd\""}
            continue
        files = {}
        if base:
            for f in names:
                if f.startswith("%s/%s/" % (PROBE_ROOT, base)) and not f.endswith("/probe.json"):
                    txt = show(root, commit, f)
                    if txt is not None and len(txt) <= PROBE_MAX_FILE:
                        files[f[len(PROBE_ROOT) + len(base) + 2:]] = txt
        p = {"id": probe_id(pid), "dir": base or "", "scenario": str(spec.get("scenario") or pid)[:300],
             "cmd": spec["cmd"], "files": files,
             "covers": [c for c in spec.get("covers", []) if isinstance(c, str)][:20] if isinstance(spec.get("covers"), list) else []}
        # Optional fields: a review finding this probe shows, its role ("safety" = the fact the change is safe because of),
        # and the way in a user takes (a screen, a CLI flag, an API route). They change how bw groups the output, never the run.
        for k in ("finding", "role", "entry", "area", "kind", "quote", "source"):
            if isinstance(spec.get(k), str) and spec[k].strip():
                p[k] = spec[k].strip()[:300]
        if isinstance(spec.get("functions"), list):  # optional: the functions the probe drives, for the hollow check
            p["functions"] = [f for f in spec["functions"] if isinstance(f, str) and re.match(r"^[A-Za-z_]\w*$", f)][:20]
        out[p["id"]] = p
    return [out[k] for k in sorted(out)]


def probe_key(p):
    return hashlib.sha1(json.dumps({k: p.get(k) for k in ("dir", "cmd", "files")}, sort_keys=True).encode()).hexdigest()[:16]


def _shell():
    if os.name == "nt":
        return None
    return shutil.which("bash") or "/bin/sh"


def code_copy(root, bwd, commit):
    """A clean copy of the code at a commit (git archive), cached by tree; installed dependency folders linked in."""
    import tarfile
    tree = git(["rev-parse", commit + "^{tree}"], root).strip()
    base = os.path.join(bwd, "wt")
    path = os.path.join(base, tree[:16])
    if not os.path.isdir(path):
        os.makedirs(base, exist_ok=True)
        tmp = path + ".part-%d" % os.getpid()
        os.makedirs(tmp, exist_ok=True)
        p = subprocess.Popen(["git", "archive", "--format=tar", commit], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:  # Python before 3.12
                tf.extractall(tmp)
        p.wait()
        for dname in DEP_DIRS:
            src = os.path.join(root, dname)
            if os.path.isdir(src) and not os.path.lexists(os.path.join(tmp, dname)):
                try:
                    os.symlink(src, os.path.join(tmp, dname), target_is_directory=True)
                except OSError:
                    pass
        setup = os.path.join(root, ".bw", "probe-setup.sh")
        if os.path.isfile(setup) and _shell():
            subprocess.run([_shell(), setup], cwd=tmp, env=dict(os.environ, BW_REPO=root, BW_TREE=tmp),
                           capture_output=True, timeout=PROBE_BUDGET)
        try:
            os.rename(tmp, path)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
        # keep the 8 most recent copies; they are bw's own cache
        olds = sorted((d for d in os.listdir(base) if ".part-" not in d), key=lambda d: os.path.getmtime(os.path.join(base, d)))
        for d in olds[:-8]:
            shutil.rmtree(os.path.join(base, d), ignore_errors=True)
    else:
        os.utime(path, None)
    return tree, path


def normalize_out(text, tree, root):
    text = text.replace(tree, "<ROOT>").replace(root, "<REPO>")
    for rx, rep in NORMALIZE:
        text = rx.sub(rep, text)
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if len(lines) > 60:
        lines = lines[:60] + ["… (%d more lines)" % (len(lines) - 60)]
    return "\n".join(lines)[:4000]


def run_probe(root, tree_path, p, timeout=None):
    pdir = os.path.join(tree_path, PROBE_ROOT, p["dir"]) if p.get("dir") else None
    if pdir:
        shutil.rmtree(pdir, ignore_errors=True)
        for rel, txt in p.get("files", {}).items():
            dest = os.path.join(pdir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as f:
                f.write(txt)
    env = dict(os.environ, TZ="UTC", NO_COLOR="1", TERM="dumb", PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
               BW_REPO=root, BW_TREE=tree_path, BW_PROBE=os.path.join(PROBE_ROOT, p["dir"]) if p.get("dir") else PROBE_ROOT)
    env.pop("FORCE_COLOR", None)
    t = time.time()
    try:
        r = subprocess.run(p["cmd"], shell=True, executable=_shell(), cwd=tree_path, env=env, capture_output=True,
                           text=True, errors="replace", timeout=timeout or PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        code, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", "timed out after %ds" % (timeout or PROBE_TIMEOUT)
    text = out if code == 0 else out + ("\n" if out else "") + err[-1500:]
    return {"out": normalize_out(text, tree_path, root) + "\n[exit %d]" % code, "exit": code, "ms": int((time.time() - t) * 1000)}


def observe(root, bwd, commit, p, reps=1, cache=None):
    """The probe's outcome at a commit, cached by (code tree, probe). reps=2 also says whether it is deterministic."""
    tree, path = code_copy(root, bwd, commit)
    key = "%s:%s" % (tree[:16], probe_key(p))
    hit = (cache or {}).get(key)
    if hit and (reps < 2 or "stable" in hit):
        return hit
    a = run_probe(root, path, p)
    res = {"out": a["out"], "exit": a["exit"], "ms": a["ms"]}
    if reps >= 2 and a["exit"] != 124:
        b = run_probe(root, path, p)
        res["stable"] = b["out"] == a["out"]
        if not res["stable"]:
            res["variants"] = [a["out"], b["out"]]
    if cache is not None and a["exit"] != 124:
        cache[key] = res
    return res


def _obs_cache(bwd):
    c = load(os.path.join(bwd, "obs.json"), {})
    return c if isinstance(c, dict) else {}


def _obs_save(bwd, c):
    if len(c) > 3000:
        c = dict(list(c.items())[-2000:])
    save(os.path.join(bwd, "obs.json"), c)


def receipt(root, bwd, a, b, probes=None, budget=None):
    """Every probe (as written at b) run on the code at a and at b. Rows: before/after output, changed, stable."""
    probes = load_probes(root, b) if probes is None else probes
    cache = _obs_cache(bwd)
    t0, budget = time.time(), budget or PROBE_BUDGET
    rows = []
    for p in probes:
        if p.get("bad"):
            rows.append({"probe": p["id"], "scenario": "", "bad": p["bad"]})
            continue
        if time.time() - t0 > budget:
            rows.append({"probe": p["id"], "scenario": p["scenario"], "skipped": "bw ran out of time (BW_PROBE_BUDGET=%ds)" % budget})
            continue
        before = observe(root, bwd, a, p, 1, cache)
        after = observe(root, bwd, b, p, 2, cache)
        if before["out"] != after["out"] and "stable" not in before:
            before = observe(root, bwd, a, p, 2, cache)  # confirm the before side too when they differ
        stable = after.get("stable", True) and before.get("stable", True)
        slow = 124 in (before["exit"], after["exit"])  # a timeout says nothing about behavior
        v = verdict(before, after, stable)
        row = {"probe": p["id"], "scenario": p["scenario"], "cmd": p["cmd"], "covers": p["covers"],
               "before": before["out"], "after": after["out"], "changed": v == "changed",
               "stable": stable, "timeout": slow, "verdict": v, "ms": before["ms"] + after["ms"]}
        if v == "changed" and before["exit"] == 0 and after["exit"] != 0:
            row["crash"] = 1  # it ran before and fails now: a real break, or a probe tied to code that moved
        if v == "cant_tell":
            row["why"] = cant_tell_why(before, after)
        for k in ("finding", "role", "entry"):
            if p.get(k):
                row[k] = p[k]
        rows.append(row)
    # A probe that now crashes while every other probe printed what it printed before is more likely tied to code the
    # task moved (a renamed function, a moved file) than to behavior: say so, the engineer decides.
    crashed = [r for r in rows if r.get("crash")]
    if crashed and len(crashed) == 1 and not any(r.get("changed") for r in rows if not r.get("crash")) and len([r for r in rows if "after" in r]) > 1:
        crashed[0]["pinned"] = 1
    _obs_save(bwd, cache)
    return {"from": a, "to": b, "rows": rows, "secs": round(time.time() - t0, 2)}


# Exit codes that say the probe could not run at all, so its output says nothing about behavior:
# 124 timeout (bw's own), 126 not executable, 127 command not found.
CANT_TELL_EXIT = {124, 126, 127}


def verdict(before, after, stable=True):
    """changed / same / cant_tell / flaky. A probe that could not run on either side is never shown as a change or as
    holding: it is "can't tell", with the reason."""
    if before["exit"] in CANT_TELL_EXIT or after["exit"] in CANT_TELL_EXIT:
        return "cant_tell"
    if not stable:
        return "flaky"
    return "changed" if before["out"] != after["out"] else "same"


def cant_tell_why(*obs):
    for o in obs:
        if o["exit"] == 124:
            return "timed out (raise BW_PROBE_TIMEOUT or make it faster)"
        if o["exit"] == 127:
            return "command not found on this machine"
        if o["exit"] == 126:
            return "command is not executable"
    return "could not run"


# ---- beliefs: a kept outcome, per person, re-checked at every later task ----

def person(root):
    return git(["config", "user.name"], root, check=False).strip() or os.environ.get("USER") or "you"


def beliefs_path(bwd):
    return os.path.join(bwd, "beliefs.json")


def belief_history(b, why, **kw):
    """Append-only: a belief's earlier expectation is never overwritten, it moves to history with the reason."""
    b.setdefault("history", []).append(dict({"expect": b.get("expect"), "commit": b.get("commit"), "since": b.get("at"),
                                             "until": time.time(), "why": why}, **kw))


def add_belief(root, bwd, p, expect, commit, task=None, item=None, who=None, why="kept again"):
    bl = load(beliefs_path(bwd), {"beliefs": []})
    who = who or person(root)
    for b in bl["beliefs"]:
        if b["person"] == who and b["probe"]["id"] == p["id"] and b["state"] == "holds":
            if b.get("expect") != expect or b.get("commit") != commit:
                belief_history(b, why)
            b.update({"expect": expect, "commit": commit, "at": time.time(), "probe": p, "item": item or b.get("item")})
            b.pop("broken", None)
            b.pop("unknown", None)
            save(beliefs_path(bwd), bl)
            return b
    b = {"id": "bl-%s-%s" % (p["id"][:30], uuid.uuid4().hex[:6]), "person": who, "probe": p, "expect": expect,
         "commit": commit, "at": time.time(), "task": task, "item": item, "state": "holds", "history": []}
    bl["beliefs"].append(b)
    save(beliefs_path(bwd), bl)
    return b


def check_beliefs(root, bwd, commit, changed=(), task=None, who=None, budget=None):
    """Re-run every belief's own copy of its probe at a commit (an agent cannot hide a break by editing the probe).
    Beliefs whose files changed run first. Returns (broken, checked, skipped)."""
    bl = load(beliefs_path(bwd), {"beliefs": []})
    live = [b for b in bl["beliefs"] if b["state"] == "holds" and (who is None or b["person"] == who)]
    changed = set(changed)
    live.sort(key=lambda b: 0 if set(b["probe"].get("covers") or []) & changed else 1)
    cache = _obs_cache(bwd)
    t0, budget = time.time(), budget or PROBE_BUDGET
    broken, checked, skipped = [], 0, 0
    for b in live:
        if time.time() - t0 > budget:
            skipped += 1
            continue
        o = observe(root, bwd, commit, b["probe"], 1, cache)
        checked += 1
        if o["exit"] in CANT_TELL_EXIT:
            # Could not run: says nothing about the behavior. Neither holds nor broken; the engineer sees "can't tell".
            b["unknown"] = {"commit": commit, "at": time.time(), "task": task, "why": cant_tell_why(o), "out": o["out"][-300:]}
            skipped += 1
            continue
        b.pop("unknown", None)
        if o["out"] != b["expect"]:
            if (b.get("broken") or {}).get("now") != o["out"]:
                b["broken"] = {"now": o["out"], "commit": commit, "at": time.time(), "task": task,
                               "crash": 1 if (o["exit"] != 0 and b["expect"].rstrip().endswith("[exit 0]")) else 0}
            broken.append(b)
        else:
            if b.get("broken"):  # it broke and came back: keep that, /bw:recall shows behaviors that flip back and forth
                b.setdefault("flips", []).append({"broke": b["broken"]["commit"], "broke_task": b["broken"].get("task"),
                                                  "back": commit, "back_task": task, "at": time.time()})
            b.pop("broken", None)
            b["checked"] = {"commit": commit, "at": time.time()}
    _obs_save(bwd, cache)
    save(beliefs_path(bwd), bl)
    return broken, checked, skipped


def probe_help(script=None, since="<commit before your change>"):
    script = script or os.path.abspath(__file__)
    return (
        "bw behavior probes. A probe is a tiny command that runs ONE situation through the real code and prints what happens. "
        "bw runs every probe on a clean copy of the code before the task and after it, and shows the engineer the two outputs "
        "side by side. Your words are not shown; only what the code printed.\n"
        "Write each probe as .bw/probes/<id>/probe.json, with any script it needs in the same folder:\n"
        '  {"scenario": "who does what, with which inputs; never the outcome", "cmd": "<shell command>", "covers": ["path/of/the/code/it/exercises"]}\n'
        "Rules:\n"
        "- cmd runs in the root of a clean copy of the repository at each commit, with no network. Load the code from that copy "
        "(e.g. PYTHONPATH=src python3 .bw/probes/<id>/run.py, node .bw/probes/<id>/run.js, go run ./.bw/probes/<id>), "
        "never from an installed package or another checkout. Installed dependency folders (node_modules, .venv, vendor) are linked in.\n"
        "- Print only the outcome: returned values, messages, files written, exit code. No timestamps, random ids, absolute paths "
        "or timings. Under 40 lines. Finish in a few seconds.\n"
        "- One situation per probe, at most 3 new probes per task, for the behaviors that changed or that the engineer asked about. "
        "Choose inputs where the output differs if the behavior changed: a table of 3-6 labelled inputs (typical case, both sides "
        "of the condition, the error path). Use the language and tools the repo already uses.\n"
        "- Drive the behavior through the way its users reach it (the public function, command, route), never private helpers, "
        "and print what they can observe. Optional fields: \"entry\" (the user's way in), \"role\": \"safety\" for the one fact "
        "your change is safe because of.\n"
        "- Keep existing probes. Never edit a probe to make its output match.\n"
        "Check them before you finish: python3 \"%s\" receipt --from %s\n"
        "If nothing you changed can run without a device, network or UI, say so in one line starting with NO PROBE: and the reason." % (script, since))


def probe_reason(task, regions_hit, files, acts=()):
    if regions_hit == ["first probes for this repo"]:
        regions_hit = []
        lines = ["bw: this repo has no behavior probes yet, so nothing shows what this change does when it runs.",
                 "Before finishing, add 1 to 3 probes for the behavior this task touched, so the engineer sees it executed now and "
                 "every later task re-runs them. Do not change other code in this step.",
                 probe_help(since=task["start"][:12]),
                 "The code before this task is commit %s. Changed files: %s" % (task["start"][:12], ", ".join(f["path"] for f in files[:30]) + (" ..." if len(files) > 30 else ""))]
        return "\n".join(lines)
    lines = [
        ("bw: this task changed regions the engineer marked as costly to put right (%s), and no probe shows that behavior "
         "changing." % ", ".join(regions_hit)) if regions_hit else
        "bw: this task carried out requests the engineer made in the watch room.",
        "Before finishing, add behavior probes so the engineer sees what the code does now, executed, next to what it did before. "
        "Do not change other code in this step.",
        probe_help(since=task["start"][:12]),
        "The code before this task is commit %s. Changed files: %s" % (task["start"][:12], ", ".join(f["path"] for f in files[:30]) + (" ..." if len(files) > 30 else "")),
    ]
    if acts:
        lines.insert(1, "The engineer asked for: " + " | ".join(a["want"] for a in acts) + ". Add one probe per request whose output shows the behavior as it is now.")
    return "\n".join(lines)


def no_probe_reason(text):
    m = re.search(r"NO PROBE:\s*(.+)", text or "")
    return m.group(1).strip()[:300] if m else None


def cmd_receipt(args):
    """bwcap.py receipt [--from REF] [--to REF] [--json]: run the repo's probes on two versions of the code.
    --to defaults to the working tree as it is now (uncommitted changes included), --from to HEAD."""
    root = repo_root(os.getcwd())
    if not root:
        print("bw: not inside a git repository."); return 2
    bwd = bw_dir(root)
    opt = lambda k, d=None: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else d
    b = opt("--to") or snapshot(root, "cli", "now")
    a = opt("--from") or git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).strip() or b
    a = git(["rev-parse", a], root).strip()
    b = git(["rev-parse", b], root).strip()
    probes = load_probes(root, b)
    if not probes:
        print("bw: no probes in %s/ yet.\n\n%s" % (PROBE_ROOT, probe_help()))
        return 0
    rc = receipt(root, bwd, a, b, probes)
    if "--json" in args:
        print(json.dumps(rc, indent=1))
        return 0
    print("bw receipt: %s -> %s, %d probe%s, %.1fs" % (a[:8], "working tree" if "--to" not in args else b[:8], len(probes), "" if len(probes) == 1 else "s", rc["secs"]))
    for r in rc["rows"]:
        if r.get("bad") or r.get("skipped"):
            print("\n? %s: %s" % (r["probe"], r.get("bad") or r.get("skipped")))
            continue
        mark = "CHANGED" if r["changed"] else ("CAN'T TELL: " + r.get("why", "could not run")) if r.get("verdict") == "cant_tell" else \
            ("FLAKY (output differs between runs, ignored)" if not r["stable"] else "same")
        if r.get("crash"):
            mark += " (now fails to run%s)" % (": may be tied to code that moved, every other probe is the same" if r.get("pinned") else "")
        if r.get("role") == "safety":
            mark = "SAFE-BECAUSE " + mark
        print("\n%s %s: %s" % ("*" if r["changed"] else "?" if r.get("verdict") == "cant_tell" else "-", r["probe"], mark))
        print("  situation: %s" % r["scenario"])
        if r.get("finding"):
            print("  finding: %s" % r["finding"])
        if r["changed"]:
            print("  before: " + r["before"].replace("\n", "\n          "))
            print("  after:  " + r["after"].replace("\n", "\n          "))
        else:
            print("  output: " + r["after"].replace("\n", "\n          "))
    return 0


def cmd_beliefs(args):
    """bwcap.py beliefs [--check [REF]] [--all]: list your beliefs, or re-run them (exit 1 if one broke; for git hooks/CI)."""
    root = repo_root(os.getcwd())
    if not root:
        print("bw: not inside a git repository."); return 2
    bwd = bw_dir(root)
    who = None if "--all" in args else person(root)
    if "--check" in args:
        i = args.index("--check")
        ref = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else None
        commit = git(["rev-parse", ref], root).strip() if ref else snapshot(root, "cli", "now")
        broken, checked, skipped = check_beliefs(root, bwd, commit, who=who)
        print("bw: %d belief%s checked at %s, %d broke%s." % (checked, "" if checked == 1 else "s", ref or "the working tree",
              len(broken), (", %d can't tell (could not run)" % skipped) if skipped else ""))
        for b in broken:
            print("\n! %s (%s, kept %s)" % (b["probe"]["scenario"], b["person"], time.strftime("%b %d", time.localtime(b["at"]))))
            print("  expected: " + b["expect"].replace("\n", "\n            "))
            print("  now:      " + b["broken"]["now"].replace("\n", "\n            "))
        return 1 if broken else 0
    bl = [b for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"] if b["state"] == "holds" and (who is None or b["person"] == who)]
    if not bl:
        print("bw: no beliefs yet. Keep a behavior in the watch room, or: bwcap.py keep <probe-id>")
        return 0
    for b in bl:
        st = "BROKEN since %s" % b["broken"]["commit"][:8] if b.get("broken") else \
            "can't tell at %s: %s" % (b["unknown"]["commit"][:8], b["unknown"]["why"]) if b.get("unknown") else "holds"
        print("- %s: %s (%s, kept %s at %s)" % (b["probe"]["id"], b["probe"]["scenario"], st,
              time.strftime("%b %d", time.localtime(b["at"])), (b.get("commit") or "?")[:8]))
        if "--history" in args:
            for hst in b.get("history", []):
                print("    until %s: expected %s (%s)" % (time.strftime("%b %d %H:%M", time.localtime(hst["until"])), firstline(hst.get("expect")), hst["why"]))
            for fl in b.get("flips", []):
                print("    broke at %s, came back at %s" % (fl["broke"][:8], fl["back"][:8]))
    return 0


def cmd_keep(args):
    """bwcap.py keep <probe-id> [--at REF]: record the probe's output now as your belief (no browser needed)."""
    root = repo_root(os.getcwd())
    if not root or not args:
        print("usage: bwcap.py keep <probe-id> [--at REF]"); return 2
    bwd = bw_dir(root)
    ref = args[args.index("--at") + 1] if "--at" in args else None
    commit = git(["rev-parse", ref], root).strip() if ref else snapshot(root, "cli", "now")
    p = {x["id"]: x for x in load_probes(root, commit)}.get(probe_id(args[0]))
    if not p or p.get("bad"):
        print("bw: no probe %r at %s." % (args[0], ref or "the working tree")); return 2
    o = observe(root, bwd, commit, p, 2, _obs_cache(bwd))
    if not o.get("stable", True):
        print("bw: %s gives different output on each run; fix it before keeping it." % p["id"]); return 1
    add_belief(root, bwd, p, o["out"], commit)
    print("bw: kept. You expect %s to give:\n  %s\nbw re-runs it after every agent task and tells you if it stops." % (p["id"], o["out"].replace("\n", "\n  ")))
    return 0


def cmd_task(args):
    """bwcap.py task start "<what was asked>" | task end [--agent NAME]: the same task boundaries the Claude Code hooks
    give, for any other agent or a person (Codex, Cursor, Aider, a shell script). `end` exits 3 when bw asks for probes:
    write them, then run `task end` again."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder. Run: python3 \"%s\" on" % os.path.abspath(__file__)); return 2
    agent = args[args.index("--agent") + 1] if "--agent" in args else "cli"
    sid = "cli-" + probe_id(agent)
    rest = [a for i, a in enumerate(args[1:], 1) if a != "--agent" and (i < 1 or args[i - 1] != "--agent")]
    if args[:1] == ["start"]:
        bwd = bw_dir(root)
        d, task = open_task(bwd, sid)
        if task and not task.get("ended"):
            print("bw: a task is already open (%s). Run `task end` first." % task["id"]); return 1
        hook_prompt({"session_id": sid, "cwd": root, "prompt": " ".join(rest) or "Agent task"})
        print("bw: task started at %s. Run `python3 \"%s\" task end` when the work is done." % (
            git(["rev-parse", "--short", "refs/bw/%s/start" % open_task(bwd, sid)[1]["id"]], root).strip(), os.path.abspath(__file__)))
        return 0
    if args[:1] == ["end"]:
        bwd = bw_dir(root)
        d, task = open_task(bwd, sid)
        if not task:
            print("bw: no open task. Start one with `task start \"<what was asked>\"`."); return 1
        out = hook_stop_all({"session_id": sid, "cwd": root, "last_assistant_message": " ".join(rest)}) or {}
        if out.get("decision") == "block":
            print(out["reason"])
            print("\nThen run `python3 \"%s\" task end` again (add \"NO PROBE: <reason>\" as its argument if no probe is possible)." % os.path.abspath(__file__))
            return 3
        print(out.get("systemMessage") or "bw: nothing changed.")
        return 0
    print(cmd_task.__doc__); return 2


def cmd_agents_md(args):
    """Instructions any coding agent can follow (paste into AGENTS.md, CLAUDE.md, .cursor/rules, ...)."""
    print("## Behavior probes (bw)\n")
    print("When you change behavior in this repository, add a probe so the engineer sees what the code does, executed.")
    print(probe_help().replace("bw behavior probes. ", ""))
    print("Paste the receipt's output at the end of your final message.")
    print("\nIf your tool has no bw hooks (anything but Claude Code with the bw plugin), mark the task yourself: run "
          "`python3 \"%s\" task start \"<what you were asked>\"` before changing code and "
          "`python3 \"%s\" task end` when done; if it asks for probes, write them and run `task end` again." % (os.path.abspath(__file__), os.path.abspath(__file__)))
    print("\nBefore you edit, run `python3 \"%s\" brief`: it lists the review findings the engineer picked to fix (CHANGE), the "
          "outcomes they kept (PRESERVE), what they sent back before (AVOID) and files no kept outcome covers (RISK)." % os.path.abspath(__file__))
    print("When asked to review or investigate without changing code, show each finding with a probe that has a \"finding\" field "
          "(start with `python3 \"%s\" how \"<question>\"`)." % os.path.abspath(__file__))
    return 0


# ---------- map draft ----------

CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".kt", ".kts", ".java", ".swift", ".go", ".rs", ".rb", ".php",
            ".cs", ".c", ".cc", ".cpp", ".h", ".m", ".scala", ".dart", ".vue", ".svelte", ".sql", ".ex", ".exs"}
AROUND = re.compile(r"(^|/)([^/]*tests?(ing)?[^/]*|docs?(_src|_examples)?|spec|specs|examples?|samples?|benchmarks?|scripts?|\.github|ci|build-logic|buildSrc|gradle|assets?|fixtures?|vendor|third_party|node_modules)(/|$)", re.I)
# Folders that never hold the product's own code. `scripts/`, `ci/`, `gradle/` and the like are only "around the code" while
# other code exists: a repo whose code all lives in plugin/scripts/ must still get code regions.
NOT_CODE = re.compile(r"(^|/)([^/]*tests?(ing)?[^/]*|docs?(_src|_examples)?|spec|specs|examples?|samples?|fixtures?|vendor|third_party|node_modules)(/|$)|^\.bw/", re.I)
# Put-right class from words in the path: a coarse first guess that the engineer corrects in the map file.
CLASS_WORDS = [
    ("answer", r"bill|pay|purchas|subscri|paywall|checkout|price|invoice|wallet|money|auth|login|signin|sign_in|identity|account|session|token|secret|crypt|sign|tls|ssl|cert|permission|privacy|consent|security"),
    ("migrate", r"migrat|schema|database|(^|[/_.-])db([/_.-]|$)|storage|persist|repositor|model|entity|cache|sync|serializ|protocol|api"),
]


def guess_class(path):
    low = path.lower()
    for cls, rx in CLASS_WORDS:
        if re.search(rx, low):
            return cls
    return "repair"


def pretty(name):
    name = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").replace("-", " ").split()
    return " ".join(words).strip().capitalize() or name


def _descend(code, prefix):
    """Go down through folders that hold at least 80% of the code below `prefix`."""
    for _ in range(8):
        inside = [f[len(prefix):] for f in code if f.startswith(prefix)]
        tops = {}
        for f in inside:
            if "/" in f:
                k = f.split("/", 1)[0]
                tops[k] = tops.get(k, 0) + 1
        if not tops:
            break
        top, n = max(tops.items(), key=lambda kv: kv[1])
        if n < 0.8 * len(inside):
            break
        prefix += top + "/"
    return prefix


def _groups(code, prefix):
    """One group per sub-folder; files sitting directly in `prefix` form one group keyed "" (the folder's own files)."""
    groups = {}
    for f in code:
        if f.startswith(prefix):
            rest = f[len(prefix):]
            key = rest.split("/", 1)[0] if "/" in rest else ""
            groups.setdefault(key, []).append(f)
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))


def is_code(path):
    """A source file of the product, not a test, doc, fixture, vendored file or a bw probe."""
    return os.path.splitext(path)[1].lower() in CODE_EXT and not NOT_CODE.search(path) and not TEST_PATH.search(path)


def draft_map(root, max_regions=12):
    """Regions from the folder structure alone: descend through folders that hold nearly all the code,
    make one region per sub-folder (or file), and split a region that still holds over half the code.
    No LLM; the class is a guess from path words that the engineer corrects."""
    files = [f for f in git(["ls-tree", "-r", "--name-only", "HEAD"], root, check=False).splitlines() if f]
    src = [f for f in files if os.path.splitext(f)[1].lower() in CODE_EXT and not NOT_CODE.search(f)]
    code = [f for f in src if not AROUND.search(f)] or src
    prefix = _descend(code, "")
    ranked = _groups(code, prefix)
    if ranked and len(ranked[0][1]) > 0.5 * len(code) and "/" in ranked[0][1][0][len(prefix):]:
        big = ranked[0][0]
        sub = _descend(code, prefix + big + "/")
        inner = [(prefix + big, k, fs) for k, fs in _groups(code, sub)]
        ranked = [(k, fs) for k, fs in ranked[1:]]
        split = [(sub, k, fs, big) for _, k, fs in inner]
    else:
        split = []
    entries = [(sub, k, fs, big) for sub, k, fs, big in split] + [(prefix, k, fs, None) for k, fs in ranked]
    entries.sort(key=lambda e: -len(e[2]))
    # A folder's own files, when they are the biggest group (a flat package), become one region per file:
    # path-word guesses (answer, migrate) first, at most 6, and the rest stay together.
    core = [e for e in entries if e[1] == ""]
    if core and entries[0][1] == "" and len(core[0][2]) >= 3:
        base, _, fs, parent = core[0]
        rank = {"answer": 0, "migrate": 1, "repair": 2}
        size = {}
        for line in git(["ls-tree", "-l", "HEAD", base or "."], root, check=False).splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5 and parts[3].isdigit():
                size[parts[4]] = int(parts[3])
        fs = sorted(fs, key=lambda f: (rank[guess_class(os.path.basename(f))], -size.get(f, 0), f))
        singles = [(base, os.path.basename(f), [f], parent) for f in fs[:6]]
        others = [(base, "", fs[6:], parent)] if fs[6:] else []
        entries = singles + others + [e for e in entries if e[1] != ""]
    regions = []
    for base, key, fs, parent in entries[:max_regions]:
        if key == "":  # the folder's own files, not its sub-folders
            folder = base.rstrip("/").split("/")[-1] if base else os.path.basename(root)
            name = ("Other %s files" if any(r["paths"][0].startswith(base) and "/" not in r["paths"][0][len(base):] and "*" not in r["paths"][0] for r in regions) else "%s (core files)") % pretty(folder if folder.lower() not in ("src", "lib", "main", "kotlin", "java") else (parent or os.path.basename(root)))
            regions.append({"name": name, "class": guess_class(" ".join(os.path.basename(f) for f in fs) if len(fs) <= 3 else ""), "layer": "code", "paths": [base + "*"]})
            continue
        generic = parent is None or parent.lower() in ("src", "lib", "app", "main", "source", "sources", "pkg", "internal", "packages", "modules")
        name = pretty(key) if generic else "%s: %s" % (pretty(parent), pretty(key))
        single = len(fs) == 1 and fs[0] == base + key
        regions.append({"name": name, "class": guess_class(key), "layer": "code", "paths": [base + key if single else base + key + "/**"]})
    if len(entries) > max_regions:
        regions.append({"name": "Other code", "class": "repair", "layer": "code", "paths": ["**"]})
    seen = {}
    for r in regions:  # two folders can get the same name; add the folder they sit in
        seen.setdefault(r["name"], []).append(r)
    for name, same in seen.items():
        if len(same) > 1:
            for r in same:
                p = r["paths"][0].rstrip("*").rstrip("/")
                where = "/".join(p.split("/")[-2:]) if p else os.path.basename(root)
                r["name"] = "%s [%s]" % (name, where)
    # No folder name says "costly" (hono, okhttp): without a fallback bw would never ask for a card.
    # Mark the two biggest code regions as migrate and say why, so the engineer can move the mark.
    if regions and not any(r["class"] in ("answer", "migrate") for r in regions):
        for r in regions[:2]:
            if r["name"] != "Other code":
                r["class"] = "migrate"
                r["why"] = "biggest code region; no folder name looked costly"
    around = [
        {"name": "Tests", "class": "undo", "layer": "around the code", "paths": ["**/*test*/**", "**/*Test/**", "**/test/**", "**/tests/**", "**/__tests__/**", "**/src/test*/**", "**/*_test.*", "**/test_*.py", "**/*.test.*", "**/*.spec.*", "**/*Test.kt", "**/*Test.java"]},
        {"name": "Docs", "class": "undo", "layer": "around the code", "paths": ["**/*.md", "**/*.rst", "docs/**", "doc/**"]},
        {"name": "Build & CI", "class": "repair", "layer": "around the code", "paths": [".github/**", "**/*.gradle*", "**/package.json", "**/pyproject.toml", "**/Cargo.toml", "**/go.mod", "**/*.lock", "**/Dockerfile", "build-logic/**", "gradle/**"]},
    ]
    return {"draft": True, "layers": ["code", "around the code"], "regions": around + regions,
            "note": "Drafted by bw from folder names. Rename regions, set each level with /bw:map (cheap, normal, costly, critical) and delete this note."}


def backfill(root, bwd, m, n):
    """Past commits as past tasks (no cards), so the timeline and co-change start with real history."""
    for line in git(["log", "--first-parent", "-n", str(n), "--format=%H%x09%ct%x09%s"], root, check=False).splitlines():
        sha, ct, subj = line.split("\t", 2)
        parent = git(["rev-parse", "-q", "--verify", sha + "^"], root, check=False).strip()
        if not parent:
            continue
        files = changed_files(root, parent, sha)
        regions = {}
        for f in files:
            r = region_of(m, f["path"])
            if r:
                regions[r] = regions.get(r, 0) + 1
        d = task_path(bwd, "commit-" + sha[:10])
        save(os.path.join(d, "summary.json"), {"id": "commit-" + sha[:10], "title": subj, "ended": int(ct),
             "regions": regions, "files": len(files), "add": sum(f["add"] for f in files),
             "del": sum(f["del"] for f in files), "cards": [], "unknown": 0, "href": ""})
        if len(files) <= BULK_FILES:
            save(os.path.join(d, "data.json"), {"regions": [{"name": r["name"], "paths": [f["path"] for f in files if region_of(m, f["path"]) == r["name"]]} for r in m.get("regions", [])], "cards": [], "unknown": [], "deleted": []})


def cmd_on(args):
    target = args[0] if args and not args[0].startswith("--") else os.getcwd()
    root = repo_root(target)
    if not root:
        print("bw: %s is not inside a git repository" % target)
        sys.exit(1)
    bwd = bw_dir(root)
    mp = os.path.join(bwd, "map.json")
    if "--map" in args:
        src = args[args.index("--map") + 1]
        given = load(src, None)
        if not given or not given.get("regions"):
            print("bw: %s is not a map file with regions (looked in %s). Nothing changed." % (src, os.path.abspath(src)))
            sys.exit(1)
        save(mp, given)
        how = "copied from " + src
    elif (load(os.path.join(root, ".bw", "map.json"), None) or load(mp, None) or {}).get("regions"):
        how = "kept the existing map"
    else:
        save(mp, draft_map(root))
        how = "drafted from your folders"
    open(os.path.join(bwd, "enabled"), "w").write(time.ctime() + "\n")
    if not os.path.exists(os.path.join(bwd, "on.json")):
        save(os.path.join(bwd, "on.json"), {"at": time.time()})
    m = load_map(root, bwd)
    backfill(root, bwd, m, 40)
    render_home(bwd, m, root)
    asks = [r["name"] for r in m.get("regions", []) if r.get("class") in ("migrate", "answer")]
    v = _vers()
    setup = ["python %s" % v["py"], "git %s" % (v["git"] or "MISSING"), "bw %s" % BW_VERSION]
    bad = [] if sys.version_info >= (3, 8) else ["python 3.8 or newer needed"]
    if not v["git"]:
        bad.append("git not found on PATH")
    print("bw is on for %s" % root)
    print("  setup     %s - %s" % (", ".join(setup), "; ".join(bad) if bad else "ok"))
    print("")
    print("  next: /bw:onboard  (10-15 min, changes no code)")
    print("  - bw scans the repo's structure, history, tests and docs, then the agent writes")
    print("    probes for the most important behaviors and bw checks each one runs.")
    print("  - You then mark each one Keep / Wrong / Skip. What you keep, bw re-checks after")
    print("    every agent task and tells you if it changes.")
    print("")
    print("  after that, work as usual: each task that changes code ends with a receipt")
    print("  (probe outputs before and after). Results: %s#catchup  (or /bw:home)" % home_url(root, bwd))
    print("  - Setup check any time: /bw:doctor")
    conf = tele_conf()
    print("")
    if "on" not in conf:
        print("  share usage counts with bw's author?  (off now)")
        print("  - only counts, versions and bw's own error codes; never paths, code, prompts,")
        print("    repo names or finding text. See exactly what: /bw:telemetry show")
        print("  - turn on: /bw:telemetry on        turn off any time: /bw:telemetry off")
    else:
        print("  telemetry is %s (/bw:telemetry)" % ("on" if tele_on() else "off"))
    print("")
    print("  turn bw off here: /bw:off     everything bw keeps is in .git/bw/ and ~/.config/bw/")


USER_CLASS = {"undo": "undo", "cheap": "undo", "repair": "repair", "fix": "repair", "normal": "repair", "migrate": "migrate",
               "costly": "migrate", "answer": "answer", "critical": "answer"}
CLASS_SAY = {"undo": "cheap", "repair": "normal", "migrate": "costly", "answer": "critical"}
CLASS_HELP = {"undo": "easy to undo; bw never asks for probes here",
              "repair": "a bug here is fixable; bw does not ask for probes",
              "migrate": "a mistake means migrating data or breaking a contract; bw asks for probes when it changes",
              "answer": "money, auth or people's data; bw asks for probes when it changes"}


def cmd_map(args):
    """bwcap.py map                -> list regions and their class
       bwcap.py map <region> <class> -> change one region's class (undo, repair, migrate/costly, answer/critical)"""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder. Type /bw:on to turn it on.")
        return
    bwd = bw_dir(root)
    mp = os.path.join(bwd, "map.json")
    m = load_map(root, bwd)
    regs = m.get("regions", [])
    words = " ".join(args).strip()
    if words:
        parts = words.rsplit(None, 1)
        cls = USER_CLASS.get(parts[-1].lower()) if len(parts) == 2 else None
        if not cls:
            print("bw: say a region (name or number) and a level, e.g. /bw:map Billing costly or /bw:map 7 critical.")
            print("     Levels: cheap, normal, costly, critical.")
            return
        want = parts[0].lower().strip("\"' ")
        if want.isdigit() and 1 <= int(want) <= len(regs):
            hit = [regs[int(want) - 1]]
        else:
            hit = [r for r in regs if r["name"].lower() == want] or [r for r in regs if want in r["name"].lower()]
        if len(hit) != 1:
            print("bw: %s region matches '%s'. Use its number from /bw:map instead." % ("no" if not hit else "more than one", parts[0]))
            return
        hit[0]["class"] = cls
        hit[0].pop("why", None)
        save(mp, m)
        render_home(bwd, m, root)
        print("bw: %s is now %s: %s." % (hit[0]["name"], CLASS_SAY[cls], CLASS_HELP[cls]))
        print("")
    print_map(regs, m.get("draft"))


def print_map(regs, draft=False):
    """The map as a person reads it: which areas make bw ask the agent for probes, and the folders each one covers."""
    print("The areas of this repo bw tracks%s. When a task changes code in a costly or critical area" % (" (guessed from folder names)" if draft else ""))
    print("and no probe shows the change, bw asks the agent to write probes before it finishes.")
    print("")
    w = max([len(r["name"]) for r in regs] + [10])
    for want, title in ((("answer", "migrate"), "bw asks for probes when these change"), (("repair", "undo"), "bw does not ask")):
        rows = [(i, r) for i, r in enumerate(regs) if r.get("class", "repair") in want]
        if not rows:
            continue
        print(title)
        for i, r in rows:
            c = r.get("class", "repair")
            paths = ", ".join(p.replace("/**", "/") for p in r.get("paths", []))
            if len(paths) > 60:
                paths = paths[:57] + "..."
            print("  %2d. %-*s  %-8s  %s%s" % (i + 1, w, r["name"], CLASS_SAY.get(c, c), paths, ("  (" + r["why"] + ")") if r.get("why") else ""))
        print("")
    print("Levels: cheap = easy to undo. normal = a bug is fixable. costly = data or contracts to migrate.")
    print("        critical = money, auth or people's data. Only costly and critical make bw ask for probes.")
    print("Change one: /bw:map <number or name> <cheap|normal|costly|critical>   e.g. /bw:map 12 costly")


def cmd_do(args):
    """Print the requests the engineer queued with "Do it now", each with its code lines, and mark them sent."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder. Type /bw:on to turn it on.")
        return
    bwd = bw_dir(root)
    sp = os.path.join(bwd, "state.json")
    st = load(sp, {})
    st.setdefault("taken", {})
    ds = {x["id"]: x for x in load(os.path.join(bwd, "decisions.json"), {"decisions": []})["decisions"]}
    todo = [k for k, w in st.get("want", {}).items() if w.get("mode") == "now" and not st["taken"].get(k) and k in ds]
    if not todo:
        print("bw: no requests waiting. In the watch room, pick Change it… then Do it now.")
        return
    now = time.time()
    print("The engineer reviewed earlier agent work in the bw watch room and asked for these changes:")
    for n, k in enumerate(todo, 1):
        x = ds[k]
        print("\n%d. %s" % (n, x["want"]))
        if x.get("question"):
            print("   Situation: %s Outcome now: %s" % (x["question"], x.get("behavior") or "?"))
        for stp in x.get("now", [])[:6]:
            print("   now: %s (%s)" % (stp.get("t", ""), stp.get("at", "")))
        if x.get("files"):
            print("   Files: %s" % ", ".join(x["files"][:6]))
        st["taken"][k] = now
    save(sp, st)
    use(bwd, "do", n=len(todo))
    save(os.path.join(bwd, "acting.json"), {"at": now, "acts": [{"id": k, "want": ds[k]["want"]} for k in todo]})


def cmd_stats(args):
    """What the use counter recorded: runs, catch-ups with something for you, pages opened, decisions made."""
    root = repo_root(args[0] if args else os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder. Type /bw:on to turn it on.")
        return
    bwd = bw_dir(root)
    ev = []
    try:
        with open(os.path.join(bwd, "use.jsonl")) as f:
            ev = [json.loads(l) for l in f if l.strip()]
    except Exception:
        pass
    if not ev:
        print("bw has not counted anything yet. Counting started with version 0.10.0.")
        return
    days = {}
    for e in ev:
        d = days.setdefault(time.strftime("%a %b %d", time.localtime(e["at"])), {"runs": set(), "need": set(), "notest": set(), "opened": [], "decision": 0, "do": 0, "predicted": 0})
        if e["ev"] == "stop":  # a run's last stop speaks for the whole run
            d["runs"].add(e.get("run"))
            for key, val in (("need", e.get("items")), ("notest", e.get("notest"))):
                (d[key].add if val else d[key].discard)(e.get("run"))
        elif e["ev"] == "review":  # a review or /bw:how task ends in review mode, not in the normal stop
            r = e.get("run") or e.get("task")
            d["runs"].add(r)
            (d["need"].add if (e.get("shown") or e.get("unshown")) else d["need"].discard)(r)
        elif e["ev"] == "opened":
            if not d["opened"] or e["at"] - d["opened"][-1] > 600:  # reloads within 10 minutes count once
                d["opened"].append(e["at"])
        elif e["ev"] in ("decision", "do", "predicted"):
            d[e["ev"]] += 1
    print("%-12s %5s %9s %8s %7s %8s %9s" % ("day", "runs", "for you", "no test", "opened", "answers", "decisions"))
    tot = [0] * 6
    for k, d in days.items():
        row = [len(d["runs"]), len(d["need"]), len(d["notest"]), len(d["opened"]), d["predicted"], d["decision"]]
        tot = [a + b for a, b in zip(tot, row)]
        print("%-12s %5d %9d %8d %7d %8d %9d" % ((k,) + tuple(row)))
    print("%-12s %5d %9d %8d %7d %8d %9d" % (("total",) + tuple(tot)))
    print("runs = runs of work; for you = runs with something in the catch-up; no test = of those, runs where no test file changed and no test command ran;")
    print("opened = watch room opened through /bw:home or the link (file:// opens are not counted); answers = predictions answered.")


def cmd_off(args):
    root = repo_root(args[0] if args else os.getcwd())
    if root and enabled(root):
        os.remove(os.path.join(git_common(root), "bw", "enabled"))
        print("bw is off for %s (history kept in .git/bw)" % root)
    else:
        print("bw was not on here")


# ---------- review mode: investigate read-only, keep only findings a run shows ----------
# A review or /bw:how task changes no code. bw asks the agent once to show each finding with a probe that runs on the code
# as it is (a reproduction), runs them, and puts the executed findings in front of the engineer: Fix this / Expected / Skip.
# A finding the agent could not show stays prose and is listed apart. Picked fixes travel to the next task as part of the
# brief, and that task's receipt says whether each one's probe flipped (fixed) or not. Nothing here is LLM-judged: the agent
# chooses what to probe, bw runs it.

def findings_path(bwd):
    return os.path.join(bwd, "findings.json")


def load_findings(bwd):
    f = load(findings_path(bwd), {"findings": {}})
    return f if isinstance(f.get("findings"), dict) else {"findings": {}}


FINDING_STATUS = {"Asked to fix": "fix", "Expected": "expected", "Skipped": "skipped", "Left open": "open"}


def finding_help(script=None, start="HEAD"):
    script = script or os.path.abspath(__file__)
    return (
        "bw review mode. You looked at the code without changing it. Before you finish, show each finding with a probe "
        "that runs on the code as it is now, so the engineer sees it executed before deciding what gets fixed. "
        "Do not change any code outside .bw/probes/ in this step.\n"
        "For each finding (at most 5, most consequential first) write .bw/probes/<id>/probe.json, with any script beside it:\n"
        '  {"finding": "one line: what is wrong or surprising, in the user\'s words", "scenario": "who does what, with which inputs", '
        '"cmd": "<shell command>", "covers": ["path/of/the/code"], "entry": "how a user gets there (optional)"}\n'
        "The probe must print the problem itself (the wrong value, the missing line, the error), so that after a fix the "
        "same probe prints something else. Same rules as every bw probe: it runs in the root of a clean copy of the repository, "
        "loads the code from that copy, prints only outcomes (no timestamps, ids, absolute paths), finishes in seconds, no network. "
        "A table of 3-6 labelled inputs (typical case, both sides of the condition, the error path) shows more than one input.\n"
        "Check them: python3 \"%s\" receipt --from %s\n"
        "A finding no probe can show (needs a device, network or UI): write one line per finding in your reply: "
        "NOT SHOWN: <finding> - <reason>. Then end with your findings as before." % (script, start))


def finding_lines(text):
    return [m.strip()[:300] for m in re.findall(r"NOT SHOWN:\s*(.+)", text or "")][:10]


def review_stop(root, bwd, task, d, inp, sess):
    """Stop for a task that changed no code: ask once for finding probes, then run them and show the findings."""
    tp = os.path.join(d, "task.json")
    last = inp.get("last_assistant_message") or last_agent_text(task.get("transcript") or inp.get("transcript_path"), task["started"])
    new = changed_probe_ids(root, task.get("own_start") or task["start"], task["end"])
    if not task.get("finding_ask"):
        if new:  # it already wrote probes on its own: no need to ask
            pass
        elif len(last or "") < 150 or inp.get("stop_hook_active") or os.environ.get("BW_ASK_PROBES", "1") != "1":
            return None  # a short answer is not a review with findings
        else:
            task["finding_ask"] = 1
            task["account"] = last  # the findings as the agent wrote them, before it answers bw
            save(tp, task)
            return {"decision": "block", "reason": finding_help(start=task["start"][:12])}
    probes = {p["id"]: p for p in load_probes(root, task["end"])}
    rows = []
    cache = _obs_cache(bwd)
    t0 = time.time()
    for pid in new:
        p = probes.get(pid)
        if not p or p.get("bad"):
            continue
        if time.time() - t0 > PROBE_BUDGET:
            rows.append({"probe": pid, "scenario": p["scenario"], "finding": p.get("finding") or p["scenario"], "verdict": "cant_tell",
                         "why": "bw ran out of time", "after": "", "cmd": p["cmd"], "covers": p["covers"]})
            continue
        o = observe(root, bwd, task["end"], p, 2, cache)
        v = "cant_tell" if o["exit"] in CANT_TELL_EXIT else ("flaky" if not o.get("stable", True) else "shown")
        r = {"probe": pid, "scenario": p["scenario"], "finding": p.get("finding") or p["scenario"], "cmd": p["cmd"],
             "covers": p["covers"], "after": o["out"], "exit": o["exit"], "verdict": v}
        if p.get("entry"):
            r["entry"] = p["entry"]
        if v == "cant_tell":
            r["why"] = cant_tell_why(o)
        rows.append(r)
    _obs_save(bwd, cache)
    unshown = finding_lines(last) + [x for x in finding_lines(task.get("account")) if x not in finding_lines(last)]
    if not rows and not unshown:
        return None
    m = load_map(root, bwd)
    data = build_data(task, m, [], {"cards": []}, task.get("account") or last)
    data["findings"] = rows
    data["unshown"] = unshown
    data["review"] = 1
    save(os.path.join(d, "data.json"), data)
    fs = load_findings(bwd)
    for r in rows:
        if r["verdict"] == "shown":
            fs["findings"]["%s-f-%s" % (task["id"], r["probe"])] = {"pid": r["probe"], "task": task["id"], "finding": r["finding"],
                                                                    "covers": r["covers"], "status": "open", "at": time.time(), "out": r["after"]}
    save(findings_path(bwd), fs)
    task["ended"] = time.time()
    save(tp, task)
    save(os.path.join(d, "summary.json"), summarize(task, data))
    render_home(bwd, m, root)
    run = task.get("run") or task["id"]
    opened = sess["last"].get("opened") == run
    if not opened and os.environ.get("BW_OPEN", "1") == "1":
        sess["last"]["opened"] = run
    save(session_file(bwd, task["session"]), sess)
    shown = [r for r in rows if r["verdict"] == "shown"]
    use(bwd, "review", task=task["id"], run=task.get("run") or task["id"], shown=len(shown), unshown=len(unshown), cant=len(rows) - len(shown))
    url = home_url(root, bwd) + "#catchup"
    if not opened and os.environ.get("BW_OPEN", "1") == "1":
        open_page(url)
    return {"systemMessage": "bw: %d finding%s shown by a run%s. Pick what gets fixed: %s" % (
        len(shown), "" if len(shown) == 1 else "s", (", %d not shown" % len(unshown)) if unshown else "", url)}


def changed_probe_ids(root, a, b):
    """Probe ids whose folder or file changed between two commits (added or edited in this task)."""
    out = git(["diff", "--name-only", a, b, "--", PROBE_ROOT], root, check=False).splitlines()
    ids = []
    for n in out:
        rel = n[len(PROBE_ROOT) + 1:]
        parts = rel.split("/")
        pid = probe_id(parts[0][:-5] if len(parts) == 1 and rel.endswith(".json") else parts[0])
        if pid not in ids:
            ids.append(pid)
    return ids


def update_findings_from_state(bwd, st):
    """The engineer's call on each finding, from the watch room, into findings.json."""
    fs = load_findings(bwd)
    changed = False
    for iid, f in fs["findings"].items():
        dec = st.get("done", {}).get(iid)
        new = FINDING_STATUS.get(dec, "open") if dec else ("open" if f["status"] in ("fix", "expected", "skipped") else f["status"])
        if f["status"] in ("fixed",) and dec == "Asked to fix":
            continue  # already fixed; the decision stays as it was
        if new != f["status"]:
            f["status"] = new
            f["decided"] = time.time()
            changed = True
    if changed:
        save(findings_path(bwd), fs)


# ---------- the brief: what the agent is told before it edits, from the ledger only ----------

def make_brief(root, bwd):
    """Change / Preserve / Avoid / Risk, built from findings the engineer picked to fix, their beliefs, and past breaks they
    sent back. Only when there is something picked to fix: otherwise the per-file pre-edit hook covers kept behavior."""
    fs = load_findings(bwd)["findings"]
    fix = [(k, f) for k, f in fs.items() if f["status"] == "fix"]
    if not fix:
        return None
    me = person(root)
    bl = [b for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"] if b["person"] == me and b["state"] == "holds"]
    scope = {c for _, f in fix for c in f.get("covers", [])}
    dirs = {os.path.dirname(c) for c in scope}
    near = lambda covers: bool(set(covers) & scope) or bool({os.path.dirname(c) for c in covers} & dirs)
    keep = [b for b in bl if near(b["probe"].get("covers") or []) and b["probe"]["id"] not in {f["pid"] for _, f in fix}]
    sent_back = [x for x in load(os.path.join(bwd, "decisions.json"), {"decisions": []})["decisions"]
                 if x["kind"] == "broken" and x["decision"] in ("Asked to restore",) and near(x.get("files") or [])]
    protected = {c for b in bl for c in (b["probe"].get("covers") or [])}
    risk = sorted(c for c in scope if c not in protected)
    lines = ["bw brief (from the engineer's own decisions, nothing guessed):"]
    lines.append("CHANGE - the engineer picked these review findings to fix. Each probe prints the problem now; after your fix it must print something else:")
    for k, f in fix[:6]:
        lines.append("- %s  (probe .bw/probes/%s, prints now: %s)" % (f["finding"], f["pid"], firstline(f.get("out", ""))))
    if keep:
        lines.append("PRESERVE - outcomes the engineer kept near this code; bw re-runs them after your task:")
        for b in keep[:6]:
            lines.append("- %s -> %s  (probe %s)" % (b["probe"]["scenario"], firstline(b["expect"]), b["probe"]["id"]))
    if sent_back:
        lines.append("AVOID - earlier agent changes here that the engineer sent back as not intended:")
        for x in sent_back[:4]:
            lines.append("- %s (%s)" % (x["title"], time.strftime("%b %d", time.localtime(x["at"]))))
    if risk:
        lines.append("RISK - files in scope that no kept outcome covers, so a break there would go unseen: %s" % ", ".join(risk[:8]))
    lines.append("Fix only what was picked. Do not edit the finding probes to make them pass; bw runs its own copy.")
    return {"text": "\n".join(lines), "change": [f["pid"] for _, f in fix], "preserve": [b["probe"]["id"] for b in keep],
            "avoid": [x["id"] for x in sent_back], "risk": risk}


def firstline(x):
    ls = [l for l in str(x or "").splitlines() if l.strip() and not l.startswith("[exit")]
    return (ls[0] if ls else str(x or "").strip())[:140]


def mark_intent(root, bwd, task, rc):
    """Group receipt rows by what the engineer wanted: asked (a finding picked to fix: fixed / not fixed), kept (one of
    their beliefs), or neither (changed without being asked)."""
    fs = load_findings(bwd)
    asked = {f["pid"]: k for k, f in fs["findings"].items() if f["status"] in ("fix", "fixed") and f.get("decided", f["at"]) <= task["started"] + 5
             and (f["status"] == "fix" or f.get("fixed_in") == task["id"])}
    me = person(root)
    kept = {b["probe"]["id"] for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"] if b["person"] == me and b["state"] == "holds"}
    changed_fs = False
    for r in rc["rows"]:
        if "after" not in r:
            continue
        if r["probe"] in asked:
            r["intent"] = "asked"
            f = fs["findings"][asked[r["probe"]]]
            if r.get("verdict") == "changed":
                r["fixed"] = 1
                if f["status"] != "fixed":
                    f.update({"status": "fixed", "fixed_in": task["id"], "fixed_out": r["after"], "fixed_at": time.time()})
                    changed_fs = True
            else:
                r["fixed"] = 0
                f["tried"] = task["id"]
                changed_fs = True
        elif r["probe"] in kept:
            r["intent"] = "kept"
        elif r.get("verdict") == "changed":
            r["intent"] = "unasked"
    if changed_fs:
        save(findings_path(bwd), fs)
        # a picked fix that landed leaves the "ready for the agent" queue, however the agent was told (brief or /bw:do)
        sp = os.path.join(bwd, "state.json")
        st = load(sp, {})
        done_ids = [k for k, f in fs["findings"].items() if f.get("fixed_in") == task["id"]]
        if done_ids and isinstance(st, dict):
            for k in done_ids:
                st.setdefault("taken", {}).setdefault(k, time.time())
            save(sp, st)


def cmd_brief(args):
    """bwcap.py brief: what bw tells an agent before it edits (Change / Preserve / Avoid / Risk). Empty when no finding is picked."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder."); return 2
    b = make_brief(root, bw_dir(root))
    print(b["text"] if b else "bw: nothing picked to fix, so no brief. Pick findings in the watch room (Fix this).")
    return 0


def decide_finding(root, bwd, verb, pid, mode="cli"):
    """Record fix / expected / skip on the latest review finding shown by probe pid. Returns the finding or None."""
    fs = load_findings(bwd)
    hit = [(k, f) for k, f in fs["findings"].items() if f["pid"] == probe_id(pid)]
    if not hit:
        return None
    k, f = hit[-1]
    f["status"] = {"fix": "fix", "expected": "expected", "skip": "skipped"}[verb]
    f["decided"] = time.time()
    save(findings_path(bwd), fs)
    st = load(os.path.join(bwd, "state.json"), {})
    st.setdefault("done", {})[k] = {"fix": "Asked to fix", "expected": "Expected", "skip": "Skipped"}[verb]
    save(os.path.join(bwd, "state.json"), st)
    if verb == "expected":
        tk = load(os.path.join(bwd, "tasks", f["task"], "task.json"), {}) or {}
        p = {x["id"]: x for x in load_probes(root, tk.get("end") or "HEAD")}.get(f["pid"])
        if p:
            add_belief(root, bwd, p, f["out"], tk.get("end"), task=f["task"], item=k, why="expected from a review finding")
    use(bwd, "decision", item=k, decision=f["status"], mode=mode)
    return f


def cmd_findings(args):
    """bwcap.py findings [fix|expected|skip <probe-id>]: list review findings, or decide one without the browser."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder."); return 2
    bwd = bw_dir(root)
    fs = load_findings(bwd)
    if args and args[0] in ("fix", "expected", "skip") and len(args) > 1:
        f = decide_finding(root, bwd, args[0], args[1], mode="cli")
        if not f:
            print("bw: no finding with probe %r." % args[1]); return 2
        print("bw: %s -> %s" % (f["finding"], f["status"]))
        return 0
    if not fs["findings"]:
        print("bw: no review findings yet. Ask for a review, or /bw:how <question>.")
        return 0
    for k, f in sorted(fs["findings"].items(), key=lambda kv: kv[1]["at"]):
        print("- [%s] %s  (probe %s, %s)" % (f["status"], f["finding"], f["pid"], time.strftime("%b %d", time.localtime(f["at"]))))
    return 0


# ---------- why: when did a behavior become what it is, and who asked for it ----------

def cmd_why(args):
    """bwcap.py why <probe-id> [--max N]: run the probe back through history to the commit where its output became what
    it is now, then show what was written at that point: bw's own task (the prompt, the engineer's decision), the commit
    message and PR link. Every line is a quote or a run; nothing is inferred."""
    root = repo_root(os.getcwd())
    if not root or not args:
        print("usage: bwcap.py why <probe-id> [--max N]"); return 2
    bwd = bw_dir(root)
    mx = int(args[args.index("--max") + 1]) if "--max" in args else 14
    now = snapshot(root, "cli", "now")
    pid = probe_id(args[0])
    p = {x["id"]: x for x in load_probes(root, now)}.get(pid)
    if not p:
        bel = [b for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"] if b["probe"]["id"] == pid]
        p = bel[-1]["probe"] if bel else None
    if not p or p.get("bad"):
        print("bw: no probe %r in %s/ or in your beliefs." % (args[0], PROBE_ROOT)); return 2
    cache = _obs_cache(bwd)
    runs = [0]

    def out_at(c):
        runs[0] += 1
        o = observe(root, bwd, c, p, 1, cache)
        return None if o["exit"] in CANT_TELL_EXIT else o["out"]

    cur = out_at(now)
    head = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).strip()
    print("bw why: %s\n  situation: %s\n  prints now: %s" % (pid, p["scenario"], (cur or "(can't run)").replace("\n", "\n              ")))
    found = None  # (commit where it became cur, output before it)
    if head and out_at(head) != cur:
        found = ("working tree", out_at(head))
    elif head:
        chain = git(["rev-list", "--first-parent", "--max-count=4000", head], root).split()
        # back off 1, 2, 4, ... first-parent steps until the output differs, then bisect the gap
        lo, hi, step = 0, None, 1
        while runs[0] < mx:
            i = min(lo + step, len(chain) - 1)
            if i == lo:
                break
            if out_at(chain[i]) == cur:
                lo, step = i, step * 2
            else:
                hi = i
                break
        while hi is not None and hi - lo > 1 and runs[0] < mx:
            mid = (lo + hi) // 2
            if out_at(chain[mid]) == cur:
                lo = mid
            else:
                hi = mid
        if hi is not None and hi - lo == 1:
            found = (chain[lo], out_at(chain[hi]))
        elif hi is None:
            print("\nThe output is the same at every commit bw ran it on, back to %s (%d runs)." % (chain[lo][:8], runs[0]))
        else:
            print("\nbw narrowed it to the commits between %s and %s (run limit %d reached; use --max)." % (chain[hi][:8], chain[lo][:8], mx))
    _obs_save(bwd, cache)
    if found:
        c, before = found
        print("\nBecame this at: %s" % (c if c == "working tree" else c[:10]))
        print("  printed before: %s" % ((before or "(can't run there)").replace("\n", "\n                  ")))
        if c != "working tree":
            meta = git(["log", "-1", "--format=%an%x1f%ad%x1f%s%x1f%b", "--date=short", c], root).split("\x1f")
            print("  commit: %s, %s: %s" % (meta[1], meta[0], meta[2]))
            body = [l for l in (meta[3] if len(meta) > 3 else "").strip().splitlines() if l.strip()][:6]
            if body:
                print("  message body:\n    " + "\n    ".join(body))
            prs = sorted(set(re.findall(r"#(\d+)", meta[2] + " " + (meta[3] if len(meta) > 3 else ""))))
            print("  PR/issue links in the message: %s" % (", ".join("#" + x for x in prs) if prs else "none"))
        print("\nbw's own record:")
        hits = tasks_for_change(root, bwd, c, pid)
        for t in hits:
            print("  task %s (%s): \"%s\"%s" % (t["id"], t["when"], t["prompt"], t["how"]))
        if not hits:
            print("  no task bw watched made this change (bw was off, or it was made by hand or before bw).")
    decs = probe_decisions(bwd, pid)
    if decs:
        print("\nYour decisions on this probe:")
        for x in decs:
            print("  %s %s" % (x[0], x[1]))
    print("\nChecked: %d run%s of the probe; git log; bw tasks and decisions. Anything not listed above was not found." % (runs[0], "" if runs[0] == 1 else "s"))
    return 0


def tasks_for_change(root, bwd, commit, pid):
    """bw tasks linked to a change: the task during which the commit was made (HEAD moved past it), or whose receipt
    showed this probe change."""
    out = []
    tdir = os.path.join(bwd, "tasks")
    for tid in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        tk = load(os.path.join(tdir, tid, "task.json"), None)
        if not tk or not tk.get("start") or not tk.get("end"):
            continue
        how = ""
        data = load(os.path.join(tdir, tid, "data.json"), {}) or {}
        rrow = next((r for r in ((data.get("receipt") or {}).get("rows") or []) if r.get("probe") == pid and r.get("changed")), None)
        if rrow:
            how = " - its receipt showed this probe change" + (" (you asked for it)" if rrow.get("intent") == "asked" else
                                                                 " (not asked)" if rrow.get("intent") == "unasked" else "")
        elif commit != "working tree":
            h0 = git(["rev-parse", "-q", "--verify", tk.get("own_start", tk["start"]) + "^"], root, check=False).strip()
            h1 = git(["rev-parse", "-q", "--verify", tk["end"] + "^"], root, check=False).strip()
            if h1 and subprocess.run(["git", "merge-base", "--is-ancestor", commit, h1], cwd=root).returncode == 0 and \
                    (not h0 or subprocess.run(["git", "merge-base", "--is-ancestor", commit, h0], cwd=root).returncode != 0):
                how = " - the commit was made during this task"
        if how:
            out.append({"id": tid, "when": time.strftime("%b %d %H:%M", time.localtime(tk.get("started", 0))),
                        "prompt": task_title(tk)[:120], "how": how})
    return out


def probe_decisions(bwd, pid):
    out = []
    for k, f in load_findings(bwd)["findings"].items():
        if f["pid"] == pid:
            out.append((time.strftime("%b %d", time.localtime(f["at"])), "review finding \"%s\": %s" % (f["finding"], f["status"])))
    for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"]:
        if b["probe"]["id"] != pid:
            continue
        for hst in b.get("history", []):
            out.append((time.strftime("%b %d", time.localtime(hst["until"])), "%s: belief changed (%s)" % (b["person"], hst["why"])))
        out.append((time.strftime("%b %d", time.localtime(b["at"])), "%s kept: %s (%s)" % (b["person"], firstline(b["expect"]), b["state"])))
    return sorted(out)


# ---------- recall: one area's state, from bw's own ledger ----------

def cmd_recall(args):
    """bwcap.py recall [region|path]: where things stand in one area - open findings, fixes and whether they held,
    beliefs (holding, broken, can't tell, flipped back), changes nobody asked for, and the last tasks there."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder."); return 2
    bwd = bw_dir(root)
    m = load_map(root, bwd)
    q = " ".join(args).strip()
    reg = next((r for r in m.get("regions", []) if q and r["name"].lower() == q.lower()), None)
    if reg:
        inside = lambda path: region_of(m, path) == reg["name"]
        label = "region %s" % reg["name"]
    elif q:
        qq = q.rstrip("/")
        inside = lambda path: path == qq or path.startswith(qq + "/")
        label = q
    else:
        inside = lambda path: True
        label = "the whole repo"
    hit = lambda covers: any(inside(c) for c in (covers or [])) or (not q)
    me = person(root)
    print("bw recall: %s (from bw's records; nothing inferred)" % label)
    fs = [f for f in load_findings(bwd)["findings"].values() if hit(f.get("covers"))]
    groups = {}
    for f in fs:
        groups.setdefault(f["status"], []).append(f)
    print("\nFindings: " + (", ".join("%d %s" % (len(v), k) for k, v in sorted(groups.items())) or "none"))
    for k in ("fix", "open", "fixed"):
        for f in groups.get(k, [])[-5:]:
            print("  [%s] %s (probe %s)%s" % (k, f["finding"], f["pid"], ", fixed in task %s" % f["fixed_in"] if f.get("fixed_in") else
                                          ", last try %s did not flip it" % f["tried"] if f.get("tried") else ""))
    bl = [b for b in load(beliefs_path(bwd), {"beliefs": []})["beliefs"] if b["person"] == me and b["state"] == "holds" and hit(b["probe"].get("covers"))]
    print("\nYour beliefs: %d" % len(bl))
    for b in bl:
        st = "BROKEN since %s" % b["broken"]["commit"][:8] if b.get("broken") else "can't tell (%s)" % b["unknown"]["why"] if b.get("unknown") else "holds"
        extra = []
        if b.get("flips"):
            extra.append("broke and came back %dx" % len(b["flips"]))
        if b.get("history"):
            extra.append("changed %dx" % len(b["history"]))
        print("  %s: %s -> %s [%s%s]" % (b["probe"]["id"], b["probe"]["scenario"][:80], firstline(b["expect"]), st, "; " + ", ".join(extra) if extra else ""))
    # recent tasks in the area, newest last, with any receipt rows nobody asked for
    tdir = os.path.join(bwd, "tasks")
    rows = []
    for tid in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        data = load(os.path.join(tdir, tid, "data.json"), None)
        tk = load(os.path.join(tdir, tid, "task.json"), None)
        if not data or not tk:
            continue
        paths = [x for rg in data.get("regions", []) for x in rg.get("paths", [])] + data.get("unmapped", [])
        fl = [r for r in data.get("findings") or [] if hit(r.get("covers"))]
        if not (q and not any(inside(x) for x in paths) and not fl):
            un = [r for r in ((data.get("receipt") or {}).get("rows") or []) if r.get("intent") == "unasked" and hit(r.get("covers"))]
            rows.append((tk.get("started", 0), task_title(tk), len([x for x in paths if inside(x)]), len(fl), un))
    print("\nLast tasks here:")
    for t in rows[-6:]:
        print("  %s  %s  (%d file%s%s)" % (time.strftime("%b %d %H:%M", time.localtime(t[0])), t[1][:90], t[2], "" if t[2] == 1 else "s",
                                           ", %d finding%s" % (t[3], "" if t[3] == 1 else "s") if t[3] else ""))
        for r in t[4][:3]:
            print("     changed, not asked: %s" % r["scenario"][:100])
    if not rows:
        print("  none")
    nxt = groups.get("open") or []
    brk = [b for b in bl if b.get("broken")]
    print("\nWaiting on you: %s" % ("; ".join(x for x in ("%d finding%s to decide" % (len(nxt), "" if len(nxt) == 1 else "s") if nxt else "",
                                                       "%d broken belief%s" % (len(brk), "" if len(brk) == 1 else "s") if brk else "") if x) or "nothing"))
    return 0


MUTATIONS = [(r"==", "!="), (r"!=", "=="), (r"<=", "<"), (r">=", ">"), (r"(?<![<>=!-])<(?![<=])", "<="), (r"(?<![<>=-])>(?![>=])", ">="),
             (r"\btrue\b", "false"), (r"\bfalse\b", "true"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
             (r"\band\b", "or"), (r"\bor\b", "and"), (r"&&", "||"), (r"\|\|", "&&")]
SKIP_MUT = re.compile(r"^\s*(#|//|\*|/\*|import |from |package |\"\"\"|''')")


def text_lines(text):
    """Line indexes that are prose, not code: inside strings that span lines (Python triple quotes, JS backticks).
    A break planted there can never change what the code does."""
    out, inside = set(), None
    for i, line in enumerate(text.split("\n")):
        if inside:
            out.add(i)
            if line.count(inside) % 2 == 1:
                inside = None
            continue
        for q in ('"""', "'''", "`"):
            if line.count(q) % 2 == 1:
                out.add(i)
                inside = q
                break
    return out


def code_mask(line):
    """The line with the inside of every quoted string and any trailing comment blanked out (same length), so a planted
    break lands on code, never on text such as a message or a regex."""
    out, q, esc = [], None, False
    for ch in line:
        if q:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == q:
                q = None
                out.append(ch)
                continue
            out.append(" ")
        else:
            if ch in "\"'`":
                q = ch
            out.append(ch)
    m = "".join(out)
    k = re.search(r"(^|\s)(#|//)", m)
    return m[:k.start()] + " " * (len(m) - k.start()) if k else m


def mutate(line, rx, rep):
    """The line with one break planted at the first match of rx that lies in code (see code_mask); None when every
    match is inside text."""
    m = re.search(rx, code_mask(line))
    if not m:
        return None
    return line[:m.start()] + re.sub(rx, rep, line[m.start():m.end()], count=1) + line[m.end():]


FUNC_START = re.compile(r"^(\s*)(?:(?:export|public|private|protected|internal|static|async|override|suspend|pub)\s+)*"
                        r"(?:def|function|func|fn|fun|sub)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
                        r"|^(\s*)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)")


def func_blocks(text):
    """{name: [(first, last) line index]} for functions in any common language: a block runs from its `def`/`function`/
    `func`/`fn` line to the line before the next non-blank line indented no deeper (a closing brace stays in)."""
    lines = text.split("\n")
    out = {}
    for i, line in enumerate(lines):
        m = FUNC_START.match(line)
        if not m:
            continue
        ind, name = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
        j = i + 1
        while j < len(lines):
            l = lines[j]
            if l.strip() and len(l) - len(l.lstrip()) <= len(ind):
                if l.strip()[0] in "})]":
                    j += 1
                break
            j += 1
        out.setdefault(name, []).append((i, j - 1))
    return out


def aimed_lines(texts, probe_text, only=None):
    """Line indexes inside the functions a probe names, plus the functions those call (one step), per covered file.
    Empty when the probe names none of them: then breaks are spread over the whole file."""
    words = set(re.findall(r"[A-Za-z_]\w{2,}", probe_text))
    blocks = {c: func_blocks(t) for c, t in texts.items()}
    chosen = {c: {n for n in b if n in (set(only) if only else words)} for c, b in blocks.items()}
    for c, t in texts.items():  # one step down: functions the named ones call
        lines = t.split("\n")
        calls = set()
        for n in chosen[c]:
            for a, z in blocks[c][n]:
                calls |= set(re.findall(r"([A-Za-z_]\w*)\s*\(", "\n".join(lines[a:z + 1])))
        chosen[c] |= {n for n in blocks[c] if n in calls}
    out = {}
    for c in texts:
        idx = set()
        for n in chosen[c]:
            for a, z in blocks[c][n]:
                idx |= set(range(a + 1, z + 1))  # the body, not the signature
        if idx:
            out[c] = (idx, sorted(chosen[c]))
    return out


def hollow_run(root, bwd, now, p, n=6):
    """Plant up to n small breaks in the code probe p covers (on throwaway copies of commit now); report which ones the
    probe's output caught. {"covers", "tried": [(file, line, src, hit)], "caught": [(file, line)], "aim"} or {"error"}."""
    covers = [c for c in p.get("covers") or [] if show(root, now, c) is not None]
    if not covers:
        return {"error": "probe %s names no file that exists in \"covers\", so bw cannot plant breaks." % p["id"]}
    tree, base = code_copy(root, bwd, now)
    ref = run_probe(root, base, p)
    if ref["exit"] in CANT_TELL_EXIT:
        return {"error": "the probe does not run (%s)." % cant_tell_why(ref)}
    texts = {c: show(root, now, c) or "" for c in covers}
    aim = aimed_lines(texts, p["cmd"] + "\n" + "\n".join(p.get("files", {}).values()), p.get("functions"))
    cands = []
    for c in covers:
        prose = text_lines(texts[c])
        for i, line in enumerate(texts[c].split("\n")):
            if SKIP_MUT.match(line) or len(line) > 300 or i in prose:
                continue
            if aim and (c not in aim or i not in aim[c][0]):
                continue
            for rx, rep in MUTATIONS:
                if mutate(line, rx, rep) is not None:
                    cands.append((c, i, rx, rep, line.strip()[:100]))
                    break
    random.Random(p["id"]).shuffle(cands)
    import tempfile
    caught, tried = [], []
    for c, i, rx, rep, src in cands[:n]:
        tmp = tempfile.mkdtemp(prefix="bw-mut-")
        try:
            work = os.path.join(tmp, "t")
            shutil.copytree(base, work, symlinks=True)
            fp = os.path.join(work, c)
            lines = open(fp, errors="replace").read().split("\n")
            lines[i] = mutate(lines[i], rx, rep)
            with open(fp, "w") as f:
                f.write("\n".join(lines))
            o = run_probe(root, work, p)
            hit = o["out"] != ref["out"]
            tried.append((c, i + 1, src, hit))
            if hit:
                caught.append((c, i + 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return {"covers": covers, "tried": tried, "caught": caught, "aim": aim}


def cmd_hollow(args):
    """bwcap.py hollow <probe-id> [--n 6]: plant small breaks (== to !=, < to <=, true to false, and to or) one at a time in
    the code the probe covers, on a throwaway copy, and count how many change the probe's output. None = the probe may be
    hollow: it would print the same if that code broke. A warning only: a probe can be right and still miss a planted break."""
    root = repo_root(os.getcwd())
    if not root or not args:
        print("usage: bwcap.py hollow <probe-id> [--n 6]"); return 2
    bwd = bw_dir(root)
    n = int(args[args.index("--n") + 1]) if "--n" in args else 6
    now = snapshot(root, "cli", "now")
    p = {x["id"]: x for x in load_probes(root, now)}.get(probe_id(args[0]))
    if not p or p.get("bad"):
        print("bw: no probe %r." % args[0]); return 2
    res = hollow_run(root, bwd, now, p, n)
    if res.get("error"):
        print("bw: " + res["error"]); return 2 if "names no file" in res["error"] else 1
    covers, tried, caught, aim = res["covers"], res["tried"], res["caught"], res["aim"]
    if not tried:
        print("bw: no line in %s has a comparison or boolean bw can flip." % ", ".join(covers)); return 0
    print("bw hollow check: %s\n  %d of %d planted breaks changed the output." % (p["id"], len(caught), len(tried)))
    if aim:
        names = sorted({n for _, ns in aim.values() for n in ns})
        print("  aimed at %s: %s" % ("the functions the probe lists" if p.get("functions") else "the code the probe calls",
                                     ", ".join(names[:12]) + (" …" if len(names) > 12 else "")))
    else:
        print("  the probe names no function in its covered files, so breaks were planted anywhere in them")
    for c, ln, src, hit in tried:
        print("  %s %s:%d  %s" % ("caught" if hit else "missed", c, ln, src))
    if not caught:
        print("\nMay be hollow: the probe printed the same with every break planted in the code it covers. Check that it drives "
              "the behavior through its real way in and prints what changes. (A warning only: planted breaks can miss what matters.)")
    return 0


def cmd_how(args):
    """bwcap.py how "<question>": start a read-only investigation task for agents without bw hooks; prints what to do."""
    root = repo_root(os.getcwd())
    if not root or not enabled(root):
        print("bw is not on in this folder."); return 2
    agent = args[args.index("--agent") + 1] if "--agent" in args else "cli"
    q = " ".join(a for i, a in enumerate(args) if a != "--agent" and (i == 0 or args[i - 1] != "--agent"))
    sid = "cli-" + probe_id(agent)
    bwd = bw_dir(root)
    d, task = open_task(bwd, sid)
    if task and not task.get("ended"):
        print("bw: a task is already open (%s). Run `task end` first." % task["id"]); return 1
    hook_prompt({"session_id": sid, "cwd": root, "prompt": "/bw:how " + q})
    print("bw: read-only investigation started. Answer this without changing code: %s\n" % q)
    print(finding_help(start="HEAD"))
    print("\nThen run `python3 \"%s\" task end%s`." % (os.path.abspath(__file__), " --agent " + agent if agent != "cli" else ""))
    return 0


# ---------- plan gate ----------
# Claude Code's plan mode, through bw. When the agent calls ExitPlanMode, bw turns the plan into a page, the
# engineer comments on it or approves it there, and the agent waits for that answer with `bwcap.py plan wait`,
# run in the background so Claude Code wakes it the moment the answer lands. bw adds what git can check: whether
# each file and line the plan cites exists, and once coding starts, which changed files the plan never named.

GATE_OFF = {"0", "false", "no", "off"}
PLAN_ID = re.compile(r"^[a-z0-9-]{6,40}$")
SHELL_META = re.compile(r"[;&|<>`$(){}\n\\]")
REF = re.compile(r"(?<![\w/.@-])((?:[\w@.-]+/)*[\w@-]+\.[A-Za-z][A-Za-z0-9]{0,7})(?::(\d+)(?:-(\d+))?)?(?![\w/])")
REF_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".kt", ".kts", ".java", ".swift", ".go", ".rs",
                           ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".dart", ".scala", ".vue",
                           ".svelte", ".sql", ".json", ".yml", ".yaml", ".toml", ".gradle", ".xml", ".html", ".css", ".scss",
                           ".sh", ".md", ".proto", ".graphql", ".prisma", ".tf", ".ini", ".cfg", ".lock"}


def gate_skip(inp, root):
    """Why nobody can answer a plan page in this run, or None."""
    if os.environ.get("BW_PLAN_GATE", "1").strip().lower() in GATE_OFF:
        return "BW_PLAN_GATE=0"
    if os.environ.get("CI", "").strip().lower() not in GATE_OFF | {""}:
        return "CI"
    if inp.get("permission_mode") == "bypassPermissions":
        return "bypassPermissions"
    if not root or not enabled(root):
        return "bw is off"
    return None


def plan_dir(bwd, pid):
    d = os.path.join(bwd, "plans", pid)
    os.makedirs(os.path.join(d, "replies"), exist_ok=True)
    return d


def plan_meta(bwd, pid):
    if not pid or not PLAN_ID.match(pid):
        return None
    return load(os.path.join(bwd, "plans", pid, "plan.json"), None)


def plan_title(text):
    for line in text.splitlines():
        t = re.sub(r"^#+\s*|[*_`]", "", line).strip()
        if t:
            return t[:90]
    return "Plan"


def plan_checks(root, text):
    """Every file the plan names, checked against the working tree: exists, line in range, or not there (yet)."""
    tracked = None
    refs, seen = [], set()
    for m in REF.finditer(text):
        path, a, b = m.group(1), m.group(2), m.group(3)
        if path.startswith("./"):
            path = path[2:]
        ext = os.path.splitext(path)[1].lower()
        if ext not in REF_EXT or ("/" not in path and not a and ext in (".md", ".json", ".lock")):
            continue
        key = (path, a, b)
        if key in seen:
            continue
        seen.add(key)
        full = os.path.join(root, path)
        hit = path if os.path.isfile(full) else None
        if not hit:
            if tracked is None:
                tracked = [p for p in git(["ls-files", "-co", "--exclude-standard"], root, check=False).splitlines() if p]
            ends = [p for p in tracked if p.endswith("/" + path)]
            hit = ends[0] if len(ends) == 1 else None
        r = {"ref": m.group(0), "path": hit or path}
        if not hit:
            r["st"] = "new"
            r["why"] = "not in the repo (a new file?)"
        else:
            try:
                n = sum(1 for _ in open(os.path.join(root, hit), errors="replace"))
            except OSError:
                n = 0
            lo, hi = int(a or 0), int(b or a or 0)
            if a and (hi > n or lo < 1 or lo > hi):
                r["st"] = "bad"
                r["why"] = "line %s is past the end (%d lines)" % (a if not b else "%s-%s" % (a, b), n)
            else:
                r["st"] = "ok"
                r["why"] = ("%d line%s" % (n, "" if n == 1 else "s")) if not a else "lines exist"
        refs.append(r)
    files = sorted({r["path"] for r in refs})
    return {"refs": refs, "files": files}


def ensure_server(root, bwd):
    info = server_info(bwd)
    if info:
        return info
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", root], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            time.sleep(0.05)
            info = server_info(bwd)
            if info:
                return info
    except Exception:
        pass
    return None


def plan_url(info, pid):
    return "http://127.0.0.1:%d/plans/%s/plan.html?t=%s" % (info["port"], pid, info["token"])


def wait_cmd(pid):
    return "python3 %s plan wait %s" % (shlex.quote(os.path.abspath(__file__)), pid)


def approval_note(meta, reply):
    lines = ["Approved by the engineer in bw (rev %d). Implement this revision." % meta["approved"]["rev"]]
    if reply and (reply.get("comments") or reply.get("note")):
        lines.append("Their notes with the approval are binding:")
        for c in reply.get("comments") or []:
            lines.append("- On \"%s\": %s" % (c.get("quote", "")[:100], c.get("text", "")))
        if reply.get("note"):
            lines.append("- " + reply["note"])
    return "\n".join(lines)


def plan_replies(bwd, pid):
    d = os.path.join(bwd, "plans", pid, "replies")
    out = []
    for f in sorted(os.listdir(d), key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            r = load(os.path.join(d, f), None)
            if r:
                out.append(r)
    return out


def hook_plan(inp):
    """PermissionRequest on ExitPlanMode. A new plan or revision: open it as a page and send the agent back to wait.
    The revision the engineer approved, sent again unchanged: let it through with the approval note appended."""
    root = hook_root(inp)
    if gate_skip(inp, root):
        return None
    ti = inp.get("tool_input") or {}
    text = (ti.get("plan") or "").strip()
    if not text and ti.get("planFilePath"):
        try:
            text = open(os.path.expanduser(ti["planFilePath"]), errors="replace").read().strip()
        except OSError:
            text = ""
    if not text:
        return None
    bwd = bw_dir(root)
    info = ensure_server(root, bwd)
    if not info:
        return None  # no page can take an answer: Claude Code shows its own dialog
    sid = inp.get("session_id") or "none"
    sess = load(session_file(bwd, sid), {})
    meta = plan_meta(bwd, sess.get("plan"))
    sha = hashlib.sha256(text.encode()).hexdigest()
    if meta and not meta.get("passed") and meta["revs"][-1]["sha"] == sha and (meta.get("approved") or {}).get("rev") == len(meta["revs"]):
        reply = next((r for r in reversed(plan_replies(bwd, meta["id"])) if r.get("kind") == "approval"), None)
        meta["passed"] = time.time()
        try:
            meta["commit"] = snapshot(root, "plan-" + meta["id"], "approved")
        except Exception:
            meta["commit"] = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).strip() or None
        save(os.path.join(plan_dir(bwd, meta["id"]), "plan.json"), meta)
        sess.pop("plan", None)
        sess["plan_active"] = meta["id"]
        save(session_file(bwd, sid), sess)
        use(bwd, "plan-pass", plan=meta["id"], rev=len(meta["revs"]))
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "allow", "updatedInput": dict(ti, plan=text + "\n\n---\n" + approval_note(meta, reply))}}}
    if not meta or meta.get("passed"):
        pid = time.strftime("%Y%m%d-%H%M%S").lower() + "-" + uuid.uuid4().hex[:4]
        meta = {"id": pid, "session": sid, "created": time.time(), "title": plan_title(text), "revs": [], "approved": None}
    fresh = not meta["revs"] or meta["revs"][-1]["sha"] != sha
    if fresh:
        meta["revs"].append({"n": len(meta["revs"]) + 1, "sha": sha, "at": time.time(), "text": text, "checks": plan_checks(root, text)})
        meta["title"] = plan_title(text)
        save(os.path.join(plan_dir(bwd, meta["id"]), "plan.json"), meta)
        use(bwd, "plan-rev", plan=meta["id"], rev=len(meta["revs"]))
    sess["plan"] = meta["id"]
    save(session_file(bwd, sid), sess)
    url = plan_url(info, meta["id"])
    if fresh:
        open_page(url)
    n = len(meta["revs"])
    msg = ("bw opened this plan for the engineer as a page (rev %d): %s\n"
           "They answer on that page, not in this chat. Do not call ExitPlanMode again until they answer.\n"
           "1. Run this Bash command with run_in_background: true, exactly as written (bw allows it without a prompt; it "
           "blocks until they answer): %s\n"
           "   If one is already running for this plan, do not start another.\n"
           "2. If it returns a change request: revise the plan as asked, then call ExitPlanMode with the full revised plan. "
           "bw shows it as rev %d.\n"
           "3. If it returns an approval: call ExitPlanMode again with exactly the same plan. Their notes are binding.\n"
           "Now tell the user in one line that the plan is open in their browser at that link, then wait." % (n, url, wait_cmd(meta["id"]), n + 1))
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": msg}}}


def real_python(exe):
    """The interpreter a command names is the system's Python, not a file the agent could have written: the bare name
    `python3`/`python` (found on PATH), or an absolute path that is this Python or the PATH one. `./python3`,
    `/tmp/x/python3` or any other file named python3 is not trusted."""
    if exe in ("python3", "python"):
        found = shutil.which(exe)
        return bool(found) and not os.path.realpath(found).startswith(os.path.realpath(os.getcwd()) + os.sep)
    if not os.path.isabs(exe) or os.path.basename(exe) not in ("python3", "python"):
        return False
    real = os.path.realpath(exe)
    trusted = {os.path.realpath(sys.executable)} | {os.path.realpath(w) for w in (shutil.which("python3"), shutil.which("python")) if w}
    return real in trusted


def hook_allow(inp):
    """PreToolUse on Bash: allow exactly bw's own `plan wait` / `plan reply` commands, with no shell syntax around them."""
    cmd = str((inp.get("tool_input") or {}).get("command") or "").strip()
    if not cmd or SHELL_META.search(cmd):
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if len(argv) != 5 or not real_python(argv[0]) or argv[2] != "plan" or argv[3] not in ("wait", "reply", "open"):
        return None
    if os.path.realpath(os.path.expanduser(argv[1])) != os.path.realpath(os.path.abspath(__file__)):
        return None
    if argv[3] != "open" and not PLAN_ID.match(argv[4]):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "bw plan wait"}}


def reply_markdown(meta, r):
    n = len(meta["revs"])
    at = time.strftime("%H:%M", time.localtime(r.get("at", time.time())))
    if r["kind"] == "approval" and meta.get("file"):
        head = "--- bw plan approval (rev %d, %s) ---" % (r["rev"], at)
        body = ["The engineer approved rev %d of %s. Implement it now." % (r["rev"], meta["file"])]
        if r.get("comments") or r.get("note"):
            body.append("Their notes with the approval are binding:")
    elif r["kind"] == "approval":
        head = "--- bw plan approval (rev %d, %s) ---" % (r["rev"], at)
        body = ["The engineer approved rev %d. Call ExitPlanMode again with exactly the same plan text; bw lets it through." % r["rev"]]
        if r.get("comments") or r.get("note"):
            body.append("Their notes with the approval are binding:")
    else:
        head = "--- bw plan change request (rev %d, %s) ---" % (r["rev"], at)
        body = ["The engineer asked for changes to rev %d. Revise the plan, then call ExitPlanMode with the full revised plan "
                "(bw shows it as rev %d). Do not write code yet." % (r["rev"], n + 1)] if not meta.get("file") else [
                "The engineer asked for changes to rev %d of %s. Edit that file as asked, then run `python3 %s plan open %s` "
                "to show rev %d and wait again. Do not write code yet." % (r["rev"], meta["file"], shlex.quote(os.path.abspath(__file__)),
                                                                           shlex.quote(meta["file"]), n + 1)]
    for i, c in enumerate(r.get("comments") or [], 1):
        body.append("%d. On \"%s\":" % (i, re.sub(r"\s+", " ", c.get("quote", "")).strip()[:160]))
        body.extend("   " + l for l in (c.get("text") or "").strip().splitlines())
    if r.get("note"):
        body.append("General: " + r["note"].strip())
    return "\n".join([head] + body + ["--- end ---"])


def cmd_plan(args):
    """bwcap.py plan wait <id> [--timeout S] : block until the engineer answers on the plan page; print it
       bwcap.py plan reply [<id>]           : print the newest answer again (default: this session's latest plan)
       bwcap.py plan list                   : list plans"""
    root = repo_root(os.getcwd())
    if not root:
        print("bw: not inside a git repository")
        sys.exit(1)
    bwd = bw_dir(root)
    sub = args[0] if args else "list"
    if sub == "list":
        pd = os.path.join(bwd, "plans")
        for pid in sorted(os.listdir(pd)) if os.path.isdir(pd) else []:
            m = plan_meta(bwd, pid)
            if m:
                st = "coding" if m.get("passed") else ("approved rev %d" % m["approved"]["rev"]) if m.get("approved") else "waiting"
                print("%s  rev %d  %-16s %s" % (pid, len(m["revs"]), st, m["title"]))
        return
    if sub == "open":
        return plan_open(root, bwd, args[1:])
    pid = args[1] if len(args) > 1 and not args[1].startswith("--") else None
    if not pid:
        pd = os.path.join(bwd, "plans")
        ids = sorted(os.listdir(pd)) if os.path.isdir(pd) else []
        pid = ids[-1] if ids else None
    meta = plan_meta(bwd, pid)
    if not meta:
        print("bw: no plan %s" % (pid or "yet"))
        sys.exit(1)
    if sub == "reply":
        rs = plan_replies(bwd, pid)
        print(reply_markdown(meta, rs[-1]) if rs else "bw: no answer to this plan yet")
        return
    if sub != "wait":
        print(cmd_plan.__doc__)
        return
    timeout = float(args[args.index("--timeout") + 1]) if "--timeout" in args else 0
    end = time.time() + timeout if timeout else None
    dp = os.path.join(plan_dir(bwd, pid), "delivered.json")
    while True:
        delivered = set(load(dp, []))
        rs = [r for r in plan_replies(bwd, pid) if r["n"] not in delivered]
        if rs:
            r = rs[0]
            save(dp, sorted(delivered | {r["n"]}))
            meta = plan_meta(bwd, pid)
            print(reply_markdown(meta, r), flush=True)
            use(bwd, "plan-delivered", plan=pid, kind=r["kind"])
            return
        if end and time.time() > end:
            print("bw: no answer within %g s" % timeout)
            sys.exit(3)
        time.sleep(0.5)


def plan_open(root, bwd, args):
    """bwcap.py plan open <file.md>: show a Markdown plan file as a plan page (a new revision when the file changed)
    and print what the agent does next. For plans written as files rather than through plan mode."""
    if not args:
        print("bw: say which plan file to open, e.g. /bw:plan docs/PLAN.md")
        sys.exit(1)
    path = " ".join(args).strip().strip("\"'")
    full = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    if not os.path.isfile(full):
        print("bw: no file %s" % path)
        sys.exit(1)
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(root))
    text = open(full, errors="replace").read().strip()
    if not text:
        print("bw: %s is empty" % rel)
        sys.exit(1)
    info = ensure_server(root, bwd)
    if not info:
        print("bw: could not start the local page server")
        sys.exit(1)
    pd = os.path.join(bwd, "plans")
    meta = None
    for pid in sorted(os.listdir(pd), reverse=True) if os.path.isdir(pd) else []:
        m = plan_meta(bwd, pid)
        if m and m.get("file") == rel and not m.get("passed"):
            meta = m
            break
    sha = hashlib.sha256(text.encode()).hexdigest()
    if not meta:
        pid = time.strftime("%Y%m%d-%H%M%S").lower() + "-" + uuid.uuid4().hex[:4]
        meta = {"id": pid, "file": rel, "created": time.time(), "title": plan_title(text), "revs": [], "approved": None}
    fresh = not meta["revs"] or meta["revs"][-1]["sha"] != sha
    if fresh:
        meta["revs"].append({"n": len(meta["revs"]) + 1, "sha": sha, "at": time.time(), "text": text, "checks": plan_checks(root, text)})
        meta["title"] = plan_title(text)
        meta["approved"] = None
        save(os.path.join(plan_dir(bwd, meta["id"]), "plan.json"), meta)
        use(bwd, "plan-rev", plan=meta["id"], rev=len(meta["revs"]), file=1)
    url = plan_url(info, meta["id"])
    if fresh:
        open_page(url)
    n = len(meta["revs"])
    print("bw opened %s for the engineer as a page (rev %d): %s" % (rel, n, url))
    print("They answer on that page. Do not change code for this plan until they approve it.")
    print("1. Run this Bash command with run_in_background: true, exactly as written (bw allows it without a prompt; it blocks "
          "until they answer): %s" % wait_cmd(meta["id"]))
    print("   If one is already running for this plan, do not start another.")
    print("2. If it returns a change request: edit %s as asked, then run /bw:plan %s again (or: python3 %s plan open %s) "
          "to show rev %d, and wait again." % (rel, rel, shlex.quote(os.path.abspath(__file__)), shlex.quote(rel), n + 1))
    print("3. If it returns an approval: implement the plan. Their notes are binding.")
    print("Now give the user the link in one line, then start the wait.")


def plan_post(root, bwd, body):
    pid = str(body.get("plan") or "")
    meta = plan_meta(bwd, pid)
    if not meta:
        return 404, {"error": "No such plan."}
    if meta.get("passed"):
        return 409, {"error": "The agent has already started on this plan."}
    n = len(meta["revs"])
    if int(body.get("rev") or 0) != n:
        return 409, {"error": "A newer revision is here. Reload the page.", "reload": True}
    kind = body.get("kind")
    comments = [{"b": int(c.get("b", -1)), "quote": str(c.get("quote", ""))[:400], "text": str(c.get("text", ""))[:4000]}
                for c in (body.get("comments") or []) if isinstance(c, dict) and str(c.get("text", "")).strip()][:100]
    note = str(body.get("note") or "").strip()[:4000]
    if kind not in ("changes", "approval"):
        return 400, {"error": "Unknown answer."}
    if kind == "changes" and not comments and not note:
        return 400, {"error": "Add a comment or a note first."}
    rs = plan_replies(bwd, pid)
    r = {"n": len(rs) + 1, "kind": kind, "rev": n, "at": time.time(), "comments": comments, "note": note}
    save(os.path.join(plan_dir(bwd, pid), "replies", "%d.json" % r["n"]), r)
    if kind == "approval":
        meta["approved"] = {"rev": n, "at": r["at"]}
        if meta.get("file"):  # no ExitPlanMode step: coding starts with this approval, so drift counts from here
            meta["passed"] = r["at"]
            try:
                meta["commit"] = snapshot(root, "plan-" + pid, "approved")
            except Exception:
                meta["commit"] = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False).strip() or None
    else:
        meta["approved"] = None
    save(os.path.join(plan_dir(bwd, pid), "plan.json"), meta)
    use(bwd, "plan-" + kind, plan=pid, rev=n)
    return 200, {"ok": True, "n": r["n"]}


def plan_drift(root, meta):
    """Since the approved plan was let through: changed files the plan never named, and named existing files left alone."""
    base = meta.get("commit")
    if not base:
        return None
    try:  # snapshot to snapshot, so files untracked in the real index count the same as tracked ones
        now = snapshot(root, "plan-" + meta["id"], "now")
        changed = sorted(f["path"] for f in changed_files(root, base, now))
    except Exception:
        return None
    checks = meta["revs"][-1].get("checks") or {}
    planned = set(checks.get("files", []))
    existed = {r["path"] for r in checks.get("refs", []) if r.get("st") == "ok"}
    unplanned = [p for p in changed if p not in planned and not any(p.endswith("/" + q) for q in planned)]
    untouched = sorted(p for p in existed if p not in changed)
    return {"at": time.time(), "changed": changed, "unplanned": unplanned, "untouched": untouched}


PLAN_FILE = re.compile(r"(^|/)([^/]*plan[^/]*\.md|plans/[^/]+\.md)$", re.I)


def plan_file_hint(inp):
    """When the task wrote a Markdown plan file that bw has not shown at its current text: say how to review it."""
    root = hook_root(inp)
    if not root or not enabled(root):
        return None
    bwd = bw_dir(root)
    sess = load(session_file(bwd, inp.get("session_id") or "none"), {})
    tid = (sess.get("last") or {}).get("tid")
    task = load(os.path.join(bwd, "tasks", tid or "-", "task.json"), None) if tid else None
    if not task:
        return None
    written = sorted({e["file"] for e in task.get("edits", []) if PLAN_FILE.search(e.get("file", ""))})
    shown = {}
    pd = os.path.join(bwd, "plans")
    for pid in os.listdir(pd) if os.path.isdir(pd) else []:
        m = plan_meta(bwd, pid)
        if m and m.get("file"):
            shown.setdefault(m["file"], set()).update(r["sha"] for r in m["revs"])
    out = []
    for f in written:
        full = os.path.join(root, f)
        if not os.path.isfile(full):
            continue
        sha = hashlib.sha256(open(full, errors="replace").read().strip().encode()).hexdigest()
        if sha not in shown.get(f, set()):
            out.append(f)
    if not out:
        return None
    return "bw: review %s as a page before coding: /bw:plan %s" % (", ".join(out[:2]), out[0])


def plan_drift_line(inp):
    root = hook_root(inp)
    if not root or not enabled(root):
        return None
    bwd = bw_dir(root)
    sess = load(session_file(bwd, inp.get("session_id") or "none"), {})
    meta = plan_meta(bwd, sess.get("plan_active"))
    if not meta:
        return None
    d = plan_drift(root, meta)
    if not d or not d["changed"]:
        return None
    meta["drift"] = d
    save(os.path.join(plan_dir(bwd, meta["id"]), "plan.json"), meta)
    bits = []
    if d["unplanned"]:
        bits.append("%d changed file%s the plan never named (%s%s)" % (len(d["unplanned"]), "" if len(d["unplanned"]) == 1 else "s",
                    ", ".join(d["unplanned"][:3]), " …" if len(d["unplanned"]) > 3 else ""))
    if d["untouched"]:
        bits.append("%d planned file%s untouched (%s%s)" % (len(d["untouched"]), "" if len(d["untouched"]) == 1 else "s",
                    ", ".join(d["untouched"][:3]), " …" if len(d["untouched"]) > 3 else ""))
    if not bits:
        return "bw plan: every changed file was in the approved plan."
    info = server_info(bwd)
    return "bw plan drift: " + "; ".join(bits) + (".  " + plan_url(info, meta["id"]) if info else ".")


def render_plan(bwd, root, pid):
    meta = plan_meta(bwd, pid)
    if not meta:
        return None
    data = {"id": pid, "title": meta["title"], "revs": [{"n": r["n"], "at": r["at"], "text": r["text"], "checks": r.get("checks") or {}} for r in meta["revs"]],
            "approved": meta.get("approved"), "passed": meta.get("passed"), "replies": plan_replies(bwd, pid),
            "drift": plan_drift(root, meta) if meta.get("passed") else None, "repo": os.path.basename(root)}
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "plan_template.html")) as f:
        tpl = f.read()
    out = os.path.join(plan_dir(bwd, pid), "plan.html")
    with open(out, "w") as f:
        f.write(tpl.replace("/*BW_PLAN*/null", json.dumps(data).replace("</", "<\\/")))
    return out



# ---------- onboarding: seed bw's understanding of a project on day 0 ----------
# `/bw:onboard` runs `onboard scan` (git and static facts only: structure, languages, entry points, test names per area,
# test commands, docs, churn) and hands the agent a worklist. The agent, read-only, writes probes for the most important
# behaviors of the top areas and for rules quoted from the docs, plus one sentence per area. `onboard verify` then runs every
# probe twice, checks every citation and quote against the files, plants a few breaks to catch hollow probes, and puts the
# probes that pass in front of the engineer (Keep / Wrong / Skip). Kept outputs become beliefs. bw makes no LLM call.

DOC_NAMES = re.compile(r"^(AGENTS|CLAUDE|README|CONTRIBUTING|ARCHITECTURE|DESIGN|HACKING|DEVELOPMENT|CONVENTIONS)(\.[\w.]+)?$", re.I)
DOC_PATHS = re.compile(r"(^|/)(\.claude/CLAUDE\.md|\.github/copilot-instructions\.md|\.cursorrules|\.cursor/rules/[^/]+|docs?/[^/]+\.(md|rst|txt|adoc))$", re.I)
TEST_NAME_RX = [
    (re.compile(r"\.py$"), re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)),
    (re.compile(r"\.(js|jsx|ts|tsx|mjs|cjs)$"), re.compile(r"""\b(?:it|test)\s*\(\s*['"`]([^'"`]{3,120})""")),
    (re.compile(r"\.go$"), re.compile(r"^func\s+(Test\w+)\s*\(", re.M)),
    (re.compile(r"\.(kt|kts|java|scala)$"), re.compile(r"@Test[\s\S]{0,200}?\b(?:fun|void)\s+`?([^`(\s]+(?:\s[^`(]+)*)`?\s*\(")),
    (re.compile(r"\.rs$"), re.compile(r"#\[(?:tokio::)?test\][\s\S]{0,120}?\bfn\s+(\w+)")),
    (re.compile(r"\.rb$"), re.compile(r"""^\s*(?:it|test)\s+['"]([^'"]{3,120})""", re.M)),
    (re.compile(r"\.swift$"), re.compile(r"func\s+(test\w+)\s*\(")),
    (re.compile(r"\.cs$"), re.compile(r"\[(?:Fact|Test|TestMethod|Theory)\][\s\S]{0,200}?\b(?:void|Task)\s+(\w+)\s*\(")),
    (re.compile(r"\.php$"), re.compile(r"function\s+(test\w+)\s*\(")),
    (re.compile(r"\.(ex|exs)$"), re.compile(r"""^\s*test\s+"([^"]{3,120})""", re.M)),
]
ENTRY_RX = re.compile(r"(^|/)(main\.(py|go|rs|ts|js|kt|java|swift|c|cpp)|__main__\.py|app\.(py|ts|js)|manage\.py|server\.(py|ts|js|go)|"
                      r"index\.(ts|js|tsx|jsx)|cli\.(py|ts|js|go)|Program\.cs|Application\.(kt|java)|[A-Z]\w*Application\.(kt|java)|"
                      r"MainActivity\.(kt|java)|App\.(tsx|jsx|swift|kt)|routes?\.(py|ts|js|rb|go|php)|urls\.py|lib\.rs|mod\.rs)$")
LANG_OF = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
           ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".swift": "Swift", ".rb": "Ruby",
           ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".scala": "Scala",
           ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart", ".m": "Objective-C", ".vue": "Vue", ".svelte": "Svelte", ".sql": "SQL"}
FIX_SUBJECT = re.compile(r"\b(fix(es|ed)?|bug|revert|regression|hotfix|crash|broken)\b", re.I)
ONBOARD_MAX_PROBES = 20


def onboard_dir(bwd):
    d = os.path.join(bwd, "onboard")
    os.makedirs(d, exist_ok=True)
    return d


def _area_prefix(r):
    return [p.split("*")[0] for p in r.get("paths", [])]


def _area_of(m, path):
    r = region_of(m, path)
    return r


def test_commands(root, files):
    names = set(files)
    cmds = []
    pk = load(os.path.join(root, "package.json"), None) if "package.json" in names else None
    if pk and isinstance(pk.get("scripts"), dict):
        for k in ("test", "test:unit", "check"):
            if k in pk["scripts"]:
                cmds.append("npm run %s  (%s)" % (k, str(pk["scripts"][k])[:80]))
    if names & {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py"} and any(f.endswith(".py") for f in files):
        cmds.append("pytest")
    if "go.mod" in names:
        cmds.append("go test ./...")
    if "Cargo.toml" in names:
        cmds.append("cargo test")
    if "gradlew" in names:
        cmds.append("./gradlew test")
    elif "pom.xml" in names:
        cmds.append("mvn test")
    if "Package.swift" in names:
        cmds.append("swift test")
    if "Gemfile" in names and any(f.startswith("spec/") for f in files):
        cmds.append("bundle exec rspec")
    if any(f.endswith((".csproj", ".sln")) for f in files):
        cmds.append("dotnet test")
    if "mix.exs" in names:
        cmds.append("mix test")
    if "Makefile" in names:
        try:
            if re.search(r"^test\s*:", open(os.path.join(root, "Makefile"), errors="replace").read(), re.M):
                cmds.append("make test")
        except Exception:
            pass
    return cmds


def onboard_scan(root, bwd):
    """Facts from git and the files only. Returns the scan dict and saves it to .git/bw/onboard/scan.json."""
    t0 = time.time()
    m = load_map(root, bwd)
    if not m.get("regions"):
        m = draft_map(root)
        save(os.path.join(bwd, "map.json"), m)
    files = [f for f in git(["ls-files"], root, check=False).splitlines() if f]
    code = [f for f in files if os.path.splitext(f)[1].lower() in CODE_EXT and not NOT_CODE.search(f) and not f.startswith(".bw/")]
    langs = {}
    for f in code:
        lg = LANG_OF.get(os.path.splitext(f)[1].lower())
        if lg:
            langs[lg] = langs.get(lg, 0) + 1
    areas = {r["name"]: {"name": r["name"], "paths": r.get("paths", []), "layer": r.get("layer", "code"), "files": 0, "tests": 0,
                         "test_names": [], "commits": 0.0, "fixes": 0, "hot": {}, "entries": []}
             for r in m.get("regions", []) if r.get("layer", "code") == "code"}
    tests = [f for f in files if TEST_PATH.search(f) and os.path.splitext(f)[1].lower() in CODE_EXT]
    for f in code:
        a = region_of(m, f)
        if a in areas and not TEST_PATH.search(f):
            areas[a]["files"] += 1
            if ENTRY_RX.search(f) and len(areas[a]["entries"]) < 6:
                areas[a]["entries"].append(f)
    # tests: filed under the area whose files they name (same stem) or whose folder they mirror; else "unplaced"
    stem_area = {}
    for f in code:
        a = region_of(m, f)
        if a in areas and not TEST_PATH.search(f):
            stem_area.setdefault(os.path.splitext(os.path.basename(f))[0].lower(), a)
    unplaced, tnames_total = 0, 0
    for f in tests[:4000]:
        stem = re.sub(r"(^test_|_test$|_spec$|\.test$|\.spec$|tests?$|spec$)", "", os.path.splitext(os.path.basename(f))[0], flags=re.I).lower()
        a = stem_area.get(stem)
        if not a:
            mirror = re.sub(r"(^|/)(src/)?(test|tests|spec|__tests__|androidTest|commonTest|jvmTest|iosTest)(/|$)", r"\1\2main\4", f)
            a = region_of(m, mirror) if mirror != f else None
            a = a if a in areas else (region_of(m, f) if region_of(m, f) in areas else None)
        names = []
        for frx, nrx in TEST_NAME_RX:
            if frx.search(f):
                try:
                    if os.path.getsize(os.path.join(root, f)) < 400000:
                        names = nrx.findall(open(os.path.join(root, f), errors="replace").read())
                except Exception:
                    pass
                break
        tnames_total += len(names)
        if a:
            areas[a]["tests"] += 1
            for nm in names:
                if len(areas[a]["test_names"]) < 12:
                    areas[a]["test_names"].append(nm.strip()[:100])
            areas[a]["test_count"] = areas[a].get("test_count", 0) + len(names)
        else:
            unplaced += 1
    # churn: last 12 months, at most 5,000 commits; commits over 40 files count 0.2
    log = git(["log", "--since=12 months ago", "-n", "5000", "--no-merges", "--name-only", "--format=%x00%s"], root, check=False)
    ncommits = 0
    for chunk in log.split("\0")[1:]:
        lines = [l for l in chunk.split("\n") if l.strip()]
        if not lines:
            continue
        ncommits += 1
        subj, fs = lines[0], lines[1:]
        w = 0.2 if len(fs) > 40 else 1.0
        hit = {}
        for f in fs:
            a = region_of(m, f)
            if a in areas:
                hit.setdefault(a, []).append(f)
        for a, afs in hit.items():
            areas[a]["commits"] += w
            if FIX_SUBJECT.search(subj):
                areas[a]["fixes"] += 1
            for f in afs:
                if not TEST_PATH.search(f) and os.path.splitext(f)[1].lower() in CODE_EXT:
                    areas[a]["hot"][f] = areas[a]["hot"].get(f, 0) + w
    for a in areas.values():
        a["commits"] = round(a["commits"], 1)
        a["score"] = round(a["commits"] + 2 * a["fixes"] + (0.5 * a["files"] if not ncommits else 0), 1)
        a["hot"] = [f for f, _ in sorted(a["hot"].items(), key=lambda kv: -kv[1])[:5] if os.path.exists(os.path.join(root, f))]
        if not a["hot"]:
            a["hot"] = [f for f in code if region_of(m, f) == a["name"] and not TEST_PATH.search(f)][:5]
    ranked = sorted((a for a in areas.values() if a["files"]), key=lambda a: -a["score"])
    focus = [a["name"] for a in ranked[:5]]
    docs = [f for f in files if (DOC_NAMES.match(os.path.basename(f)) and f.count("/") <= 2) or DOC_PATHS.search(f)]
    docs = sorted(docs, key=lambda f: (0 if re.match(r"(AGENTS|CLAUDE)", os.path.basename(f), re.I) else 1 if f.count("/") == 0 else 2, f))[:12]
    scan = {"at": time.time(), "head": git(["rev-parse", "--short", "HEAD"], root, check=False).strip(), "files": len(files),
            "code_files": len(code), "languages": sorted(langs.items(), key=lambda kv: -kv[1])[:6], "commits_12m": ncommits,
            "tests": len(tests), "test_names": tnames_total, "tests_unplaced": unplaced, "test_commands": test_commands(root, files),
            "docs": docs, "areas": ranked + [a for a in areas.values() if not a["files"]], "focus": focus,
            "secs": round(time.time() - t0, 1)}
    save(os.path.join(onboard_dir(bwd), "scan.json"), scan)
    return scan


def onboard_worklist(scan, script):
    out = ["bw onboarding scan of this repo (git and files only, %.1f s)" % scan["secs"],
           "  %d files, %d code files; %s; %d commits in the last 12 months; %d test files, %d test names found" % (
               scan["files"], scan["code_files"], ", ".join("%s %d" % tuple(kv) for kv in scan["languages"]) or "no code found",
               scan["commits_12m"], scan["tests"], scan["test_names"]),
           "  test commands found (not run): %s" % (", ".join(scan["test_commands"]) or "none"), "",
           "DOCS to read first:"] + ["  " + d for d in scan["docs"]] + ([""] if scan["docs"] else ["  (none found)", ""])
    out.append("FOCUS areas, most changed and most fixed first:")
    by = {a["name"]: a for a in scan["areas"]}
    for i, n in enumerate(scan["focus"]):
        a = by[n]
        out.append("  %d. %s  [%s]  %d files, %.0f commits, %d fix commits, %d test files" % (
            i + 1, n, ", ".join(p.replace("/**", "/") for p in a["paths"]), a["files"], a["commits"], a["fixes"], a["tests"]))
        if a["entries"]:
            out.append("     entry points: " + ", ".join(a["entries"][:4]))
        out.append("     most changed: " + ", ".join(a["hot"][:4]))
    others = [a["name"] for a in scan["areas"] if a["name"] not in scan["focus"] and a["files"]]
    if others:
        out.append("  other areas: " + ", ".join(others))
    out.append("")
    out.append("Next: write the probes and .bw/project.json as the /bw:onboard instructions say, then run:")
    out.append("  python3 \"%s\" onboard verify" % script)
    return "\n".join(out)


def _quote_in(text, quote):
    norm = lambda x: re.sub(r"[\s*_`>#-]+", " ", x).strip().lower()
    return bool(quote) and norm(quote) in norm(text or "")


def onboard_verify(root, bwd, budget=300):
    """Run every probe twice at the working tree, check citations, quotes and (within budget) planted breaks.
    Writes .git/bw/onboard/result.json, an onboarding task for the watch room, and onboard.html."""
    t0 = time.time()
    scan = load(os.path.join(onboard_dir(bwd), "scan.json"), None) or onboard_scan(root, bwd)
    m = load_map(root, bwd)
    now = snapshot(root, "onboard", "verify")
    probes = load_probes(root, now)
    project = load(os.path.join(root, ".bw", "project.json"), None)
    project = project if isinstance(project, list) else (project or {}).get("areas", []) if isinstance(project, dict) else []
    cache = _obs_cache(bwd)
    rows = []
    for p in probes[:40]:
        row = {"probe": p["id"], "scenario": p.get("scenario", ""), "finding": p.get("finding") or p.get("scenario", ""),
               "cmd": p.get("cmd", ""), "covers": p.get("covers", []), "area": p.get("area", ""), "kind": p.get("kind", "behavior"),
               "entry": p.get("entry", ""), "quote": p.get("quote", ""), "source": p.get("source", ""), "problems": []}
        if p.get("bad"):
            row["problems"].append(p["bad"])
            rows.append(row)
            continue
        if not row["area"]:
            regs = [region_of(m, c) for c in row["covers"]]
            row["area"] = next((r for r in regs if r), "")
        missing = [c for c in row["covers"] if show(root, now, c) is None]
        if not row["covers"]:
            row["problems"].append("names no file in \"covers\"")
        elif missing:
            row["problems"].append("covers a file that does not exist: " + ", ".join(missing[:3]))
        if row["entry"]:
            ep, eline, _ = parse_at(row["entry"])
            txt = show(root, now, ep) if ep else None
            if txt is None:
                row["problems"].append("entry %s is not a file in the repo" % row["entry"])
            elif eline and eline > txt.count("\n") + 1:
                row["problems"].append("entry %s is past the end of the file" % row["entry"])
            else:
                row["entry_ok"] = 1
        if row["kind"] == "rule":
            src = show(root, now, row["source"]) if row["source"] else None
            if not row["quote"] or src is None:
                row["problems"].append("a rule probe needs \"quote\" and the \"source\" doc it is from")
            elif not _quote_in(src, row["quote"]):
                row["problems"].append("the quoted line is not in %s" % row["source"])
        if time.time() - t0 > budget:
            row["problems"].append("bw ran out of time before running it")
            rows.append(row)
            continue
        o = observe(root, bwd, now, p, 2, cache)
        row.update(after=o["out"], exit=o["exit"], ms=o["ms"], stable=o.get("stable", True))
        if o["exit"] in CANT_TELL_EXIT:
            row["problems"].append("could not run: " + cant_tell_why(o))
        elif not o.get("stable", True):
            row["problems"].append("prints something different on each run")
        rows.append(row)
    _obs_save(bwd, cache)
    # hollow check on the probes that passed, cheapest first, within what is left of the budget
    for row in sorted([r for r in rows if not r["problems"]], key=lambda r: r.get("ms", 0)):
        if time.time() - t0 > budget * 0.8:
            continue
        p = next(x for x in probes if x["id"] == row["probe"])
        try:
            h = hollow_run(root, bwd, now, p, 3)
        except Exception as e:
            tele_error("cmd-onboard", e, bwd)
            continue
        if h.get("tried"):
            row["hollow"] = [len(h["caught"]), len(h["tried"])]
            row["missed"] = ["%s:%d  %s" % (f, n, src) for f, n, src, hit in h["tried"] if not hit]
    ok = [r for r in rows if not r["problems"]]
    bad = [r for r in rows if r["problems"]]
    for pa in project:
        if isinstance(pa, dict) and pa.get("entry"):
            ep, eline, _ = parse_at(pa["entry"])
            txt = show(root, now, ep) if ep else None
            pa["entry_ok"] = bool(txt is not None and (not eline or eline <= txt.count("\n") + 1))
    result = {"at": time.time(), "commit": now, "scan": scan, "project": project, "rows": rows,
              "ok": len(ok), "failed": len(bad), "secs": round(time.time() - t0, 1)}
    save(os.path.join(onboard_dir(bwd), "result.json"), result)
    # A new onboarding replaces the previous one's undecided items (decided ones are already beliefs or findings).
    fs = load_findings(bwd)
    st = load(os.path.join(bwd, "state.json"), {}) or {}
    for old in [t for t in os.listdir(os.path.join(bwd, "tasks")) if t.startswith("onboard-")]:
        keys = [k for k in fs["findings"] if k.startswith(old + "-f-")]
        if not any(k in (st.get("done") or {}) or fs["findings"][k]["status"] != "open" for k in keys):
            for k in keys:
                fs["findings"].pop(k, None)
            shutil.rmtree(os.path.join(bwd, "tasks", old), ignore_errors=True)
    save(findings_path(bwd), fs)
    render_onboard(bwd, root, result)
    # the probes that passed go to the watch room as onboarding items: Keep (belief), Wrong (a finding to fix), Skip
    tid = "onboard-" + time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(bwd, "tasks", tid)
    os.makedirs(d, exist_ok=True)
    findings = [dict(r, verdict="shown", finding=r["finding"]) for r in ok]
    data = {"title": "Onboarding: %d probe%s on %d area%s" % (len(ok), "" if len(ok) == 1 else "s", len({r["area"] for r in ok}),
                                                              "" if len({r["area"] for r in ok}) == 1 else "s"),
            "task": tid, "start": now[:8], "end": now, "totals": {"files": 0, "add": 0, "del": 0}, "regions": [], "unmapped": [],
            "layers": m.get("layers", []), "cards": [], "unknown": [], "loose": [], "findings": findings, "review": 1, "onboard": 1,
            "unshown": ["%s (probe %s: %s)" % (r["finding"][:120], r["probe"], "; ".join(r["problems"])) for r in bad]}
    save(os.path.join(d, "data.json"), data)
    save(os.path.join(d, "task.json"), {"id": tid, "root": root, "start": now, "end": now, "started": t0, "ended": time.time(),
                                        "prompts": ["/bw:onboard"], "mode": "onboard"})
    save(os.path.join(d, "summary.json"), {"id": tid, "title": data["title"], "ended": time.time(), "regions": {}, "files": 0,
                                           "add": 0, "del": 0, "cards": [], "unknown": 0, "href": "onboard.html", "run": tid})
    fs = load_findings(bwd)
    for r in ok:
        fs["findings"]["%s-f-%s" % (tid, r["probe"])] = {"pid": r["probe"], "task": tid, "finding": r["finding"], "covers": r["covers"],
                                                          "status": "open", "at": time.time(), "out": r.get("after", ""), "seed": 1}
    save(findings_path(bwd), fs)
    render_home(bwd, m, root)
    tele_count(bwd, {"onboard_probes_ok": len(ok), "onboard_probes_failed": len(bad)})
    return result


def render_onboard(bwd, root, res):
    """The onboarding page: what bw now knows about the project, each fact labelled by where it comes from."""
    e = lambda x: html.escape(str(x))
    scan = res["scan"]
    by_area = {}
    for r in res["rows"]:
        by_area.setdefault(r["area"] or "Other", []).append(r)
    summ = {pa.get("area"): pa for pa in res.get("project", []) if isinstance(pa, dict)}
    def probe_card(r):
        tag = ("<span class='tag ok'>ran twice, same output</span>" if not r["problems"] else "<span class='tag bad'>not used</span>")
        if r.get("hollow"):
            c, t = r["hollow"]
            tag += " <span class='tag %s'>%d of %d planted breaks caught</span>" % ("ok" if c else "warn", c, t)
        rule = ("<div class='quote'><span class='lbl'>%s says</span> “%s”</div>" % (e(r["source"]), e(r["quote"]))) if r["kind"] == "rule" and r["quote"] else ""
        out = ("<pre>%s</pre>" % e(r.get("after", ""))) if r.get("after") else ""
        probs = ("<div class='probs'>%s</div>" % "<br>".join(e(x) for x in r["problems"])) if r["problems"] else ""
        entry = (" · way in <code>%s</code>%s" % (e(r["entry"]), " ✓" if r.get("entry_ok") else "")) if r["entry"] else ""
        return ("<div class='probe'><div class='ph'><b>%s</b> %s</div><div class='sc'>%s%s</div>%s"
                "<div class='cmd'>$ %s</div>%s%s</div>" % (e(r["finding"]), tag, e(r["scenario"]), entry, rule, e(r["cmd"]), out, probs))
    areas_html = []
    for a in scan["areas"]:
        if not a["files"]:
            continue
        rs = by_area.get(a["name"], [])
        sm = summ.get(a["name"]) or {}
        say = ("<p class='say'>%s <span class='src'>agent's words%s</span></p>" % (
            e(sm.get("summary", "")), (" · way in <code>%s</code> %s" % (e(sm["entry"]), "✓ exists" if sm.get("entry_ok") else "✗ not found")) if sm.get("entry") else "")) if sm.get("summary") else ""
        tn = ", ".join(e(x) for x in a["test_names"][:4])
        areas_html.append(
            "<section class='area%s'><div class='ah'><h3>%s</h3><span class='paths'>%s</span>%s</div>%s"
            "<div class='facts'><span><b>%d</b> files</span><span><b>%.0f</b> commits in 12 months</span><span><b>%d</b> fix commits</span>"
            "<span><b>%d</b> test files%s</span><span><b>%d</b> probes</span></div>%s%s</section>" % (
                " focus" if a["name"] in scan["focus"] else "", e(a["name"]), e(", ".join(p.replace("/**", "/") for p in a["paths"])),
                "<span class='tag'>onboarded</span>" if a["name"] in scan["focus"] else "", say,
                a["files"], a["commits"], a["fixes"], a["tests"], (" (%d tests)" % a.get("test_count", 0)) if a.get("test_count") else "",
                len([r for r in rs if not r["problems"]]),
                ("<div class='tn'>Tests here, e.g. %s</div>" % tn) if tn else ("<div class='tn warn'>No tests found for this area.</div>" if a["name"] in scan["focus"] else ""),
                "".join(probe_card(r) for r in rs)))
    other = by_area.get("Other", [])
    docs = "".join("<li><code>%s</code></li>" % e(d) for d in scan["docs"]) or "<li>none found</li>"
    page = ONBOARD_PAGE.format(
        repo=e(os.path.basename(root)), head=e(scan["head"]), files=scan["files"], code=scan["code_files"],
        langs=e(", ".join("%s %d" % tuple(kv) for kv in scan["languages"])), commits=scan["commits_12m"], tests=scan["tests"],
        tnames=scan["test_names"], cmds=e(", ".join(scan["test_commands"]) or "none found"), docs=docs, ok=res["ok"], failed=res["failed"],
        nareas=len({r["area"] for r in res["rows"] if not r["problems"]}), areas="".join(areas_html) + (
            "<section class='area'><div class='ah'><h3>Not tied to an area</h3></div>%s</section>" % "".join(probe_card(r) for r in other) if other else ""),
        secs=res["secs"])
    with open(os.path.join(bwd, "onboard.html"), "w") as f:
        f.write(page)


ONBOARD_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>bw onboarding · {repo}</title><style>
:root{{--bg:#f6f7f5;--panel:#fff;--fg:#1b1f1d;--muted:#5b645f;--line:#d9dfdb;--accent:#1f6f5c;--soft:#e2f0ea;--warn:#9a5b00;--wsoft:#fbf0dc;--bad:#a23b2e;--bsoft:#f8e4e0;--code:#eef1ef}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121614;--panel:#1a201d;--fg:#e4e9e6;--muted:#9aa59f;--line:#2c3531;--accent:#5cc3a4;--soft:#183129;--warn:#e0a54a;--wsoft:#33270f;--bad:#ec8576;--bsoft:#3a1c18;--code:#202825;color-scheme:dark}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
.wrap{{max-width:960px;margin:0 auto;padding:28px 16px 60px;display:grid;gap:22px}}
h1,h2,h3{{margin:0;line-height:1.25}} h1{{font-size:26px}} h2{{font-size:18px}} h3{{font-size:16px}}
code,pre,.cmd{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}} code{{background:var(--code);padding:1px 4px;border-radius:4px}}
pre{{background:var(--code);border-radius:6px;padding:8px 10px;margin:6px 0 0;overflow-x:auto;white-space:pre-wrap}}
.sub{{color:var(--muted)}} .kpis{{display:flex;flex-wrap:wrap;gap:10px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:120px}} .kpi b{{font-size:22px;display:block}}
.cta{{background:var(--soft);border-radius:10px;padding:14px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}}
.cta a{{background:var(--accent);color:var(--bg);padding:8px 14px;border-radius:8px;text-decoration:none;font-weight:600}}
.area{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:grid;gap:8px}}
.area.focus{{border-color:var(--accent)}} .ah{{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}} .paths{{color:var(--muted);font-size:13px;font-family:ui-monospace,Menlo,monospace}}
.facts{{display:flex;flex-wrap:wrap;gap:14px;font-size:13.5px;color:var(--muted)}} .facts b{{color:var(--fg)}}
.say{{margin:0}} .src{{font-size:12px;color:var(--muted);margin-left:6px}} .tn{{font-size:13px;color:var(--muted)}} .tn.warn{{color:var(--warn)}}
.probe{{border-top:1px solid var(--line);padding-top:8px;display:grid;gap:3px}} .ph{{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}}
.sc{{font-size:13.5px;color:var(--muted)}} .cmd{{color:var(--muted)}} .quote{{font-size:13.5px;background:var(--wsoft);padding:6px 8px;border-radius:6px}}
.lbl{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}}
.probs{{color:var(--bad);font-size:13px}} .tag{{font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;background:var(--code);color:var(--muted)}}
.tag.ok{{background:var(--soft);color:var(--accent)}} .tag.warn{{background:var(--wsoft);color:var(--warn)}} .tag.bad{{background:var(--bsoft);color:var(--bad)}}
.side{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}} .side>div{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
ul{{margin:6px 0 0;padding-left:18px}}
</style></head><body><div class="wrap">
<header style="display:grid;gap:6px"><div class="sub">bw onboarding · {repo} at {head}</div><h1>What bw knows about this project</h1>
<p class="sub">Structure, history and tests come from git and the files. Each probe ran on the code twice and printed the same. Area summaries are the agent's words, marked as such; bw checked only that the files and lines they cite exist.</p></header>
<div class="kpis"><div class="kpi"><b>{ok}</b>probes that run</div><div class="kpi"><b>{nareas}</b>areas covered</div><div class="kpi"><b>{tests}</b>test files ({tnames} tests)</div><div class="kpi"><b>{commits}</b>commits, 12 months</div><div class="kpi"><b>{failed}</b>probes not used</div></div>
<div class="cta"><div><b>Next: decide on each probe.</b> Keep = this is how it should behave (bw re-checks it after every agent task and tells you if it changes). Wrong = it is a bug to fix. Skip = not interesting.</div><a href="home.html#catchup">Open the decisions</a></div>
<div class="side"><div><span class="lbl">Project</span><div>{files} files, {code} code files</div><div class="sub">{langs}</div></div>
<div><span class="lbl">Test commands found (not run)</span><div>{cmds}</div></div><div><span class="lbl">Docs read</span><ul>{docs}</ul></div></div>
<h2>Areas, most changed first</h2>
{areas}
<p class="sub">Onboarding check took {secs} s. Re-run any time: /bw:onboard</p>
</div></body></html>"""


# ---------- telemetry: opt-in, counts only, every field allowlisted ----------
# bw keeps a few daily counts in its own folder (~/.config/bw, never in a repo) so `/bw:telemetry show` and `/bw:doctor`
# can say what happened. Nothing leaves the machine unless the person runs `/bw:telemetry on`. Then, at most once a day,
# the finished days are sent as three kinds of events (install / day / error). Every event passes tele_valid(): each field
# has a type, every text value comes from a closed list written below, and an event with anything else is dropped whole.
# There is no field that can hold a path, a file or region name, code, a prompt, probe output or a finding: that is the
# privacy guarantee, not scrubbing. Error messages and tracebacks stay in the local errors.log under a short id.

BW_VERSION = "0.15.1"
TELE_HOST = os.environ.get("BW_TELEMETRY_HOST", "https://us.i.posthog.com")
TELE_KEY = os.environ.get("BW_TELEMETRY_KEY", "phc_sQTYuZwYQkN3HKFiVo2d2XZ3VJB3YUjCEzYJQWPxZB7t")  # public project key (write-only); empty = never send
TELE_SEND_EVERY = 20 * 3600
HOOK_TIMEOUT = {"prompt": 60, "post": 30, "stop": 300, "pre": 30, "allow": 10, "plan": 60, "postfail": 30}
TELE_OS = ("macos", "linux", "wsl", "windows", "other")
TELE_HOOKS = tuple(HOOK_TIMEOUT)
TELE_CMDS = ("on", "off", "home", "stats", "map", "do", "plan", "how", "why", "recall", "receipt", "beliefs", "keep", "findings",
             "brief", "hollow", "task", "agents-md", "doctor", "telemetry", "serve", "backfill", "render", "onboard")
TELE_COUNTERS = ("tasks", "reviews", "receipts", "probes_run", "changed", "same", "cant_tell", "flaky", "probe_cmd_missing",
                 "probe_timeout", "probe_skipped", "probe_ask", "first_probe_ask", "probe_ask_answered", "no_probe_reply",
                 "findings_shown", "findings_unshown", "beliefs_run", "beliefs_broke", "pages", "plan_opened", "plan_approved",
                 "plan_changes", "brief_sent", "onboard_scans", "onboard_probes_ok", "onboard_probes_failed")
TELE_DECISIONS = ("fix", "expected", "skip", "keep", "not_intended", "leave_open", "predicted", "other")
TELE_FILES = ("0", "1-3", "4-10", "11+")
TELE_RTIME = ("<10s", "<60s", "<180s", "180s+")
TELE_CAUSES = ("git_unsafe_repo", "git_missing", "git_failed", "io_error", "state_corrupt", "subprocess_timeout", "server_port",
               "hook_timeout", "python_old", "other")
TELE_GITCMDS = ("rev-parse", "archive", "diff", "ls-files", "show", "log", "config", "write-tree", "read-tree", "add",
                "update-ref", "cat-file", "status", "hash-object", "commit-tree", "for-each-ref", "other")
TELE_STAGES = TELE_HOOKS + tuple("cmd-" + c for c in TELE_CMDS) + ("receipt", "beliefs", "repo_root", "send")
_RX_VER = re.compile(r"^\d{1,4}(\.\d{1,4}){0,2}$")
_RX_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RX_REPO = re.compile(r"^r_[0-9a-f]{8}$")
_RX_INSTALL = re.compile(r"^i_[0-9a-f]{16}$")
_RX_ERRID = re.compile(r"^e[0-9a-f]{4}$")


def _exc_names():
    import builtins
    return tuple(sorted(n for n in dir(builtins) if isinstance(getattr(builtins, n), type)
                        and issubclass(getattr(builtins, n), BaseException))) + ("Other",)


TELE_EXC = _exc_names()
TELE_ERRNO = tuple(sorted(__import__("errno").errorcode.values())) + ("none",)


def _v_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 10 ** 7


def _v_enum(vals):
    return lambda x: isinstance(x, str) and x in vals


def _v_rx(rx):
    return lambda x: isinstance(x, str) and bool(rx.match(x))


def _v_counts(keys):
    return lambda x: isinstance(x, dict) and all(k in keys and _v_int(v) for k, v in x.items())


def _v_fn(x):
    g = globals().get(x)
    return isinstance(x, str) and (x in ("nested", "module") or (callable(g) and getattr(g, "__module__", None) == __name__))


TELE_SCHEMA = {
    "install": {"v": _v_rx(_RX_VER), "cc": _v_rx(_RX_VER), "py": _v_rx(_RX_VER), "git": _v_rx(_RX_VER), "os": _v_enum(TELE_OS)},
    "day": dict({k: _v_int for k in TELE_COUNTERS}, date=_v_rx(_RX_DATE), repo=_v_rx(_RX_REPO), v=_v_rx(_RX_VER),
                decisions=_v_counts(TELE_DECISIONS), cmds=_v_counts(TELE_CMDS), hooks=_v_counts(TELE_HOOKS),
                files=_v_counts(TELE_FILES), receipt_time=_v_counts(TELE_RTIME)),
    "error": {"date": _v_rx(_RX_DATE), "repo": _v_rx(_RX_REPO), "v": _v_rx(_RX_VER), "stage": _v_enum(TELE_STAGES),
              "fn": _v_fn, "line": _v_int, "type": _v_enum(TELE_EXC), "errno": _v_enum(TELE_ERRNO), "cause": _v_enum(TELE_CAUSES),
              "gitcmd": _v_enum(TELE_GITCMDS), "count": _v_int, "err_ids": lambda x: isinstance(x, list) and len(x) <= 5
              and all(_v_rx(_RX_ERRID)(i) for i in x)},
}
TELE_REQUIRED = {"install": ("v", "os"), "day": ("date", "repo", "v"), "error": ("date", "v", "stage", "cause", "count")}


def tele_valid(ev):
    """True only if every field of the event is in the schema and passes its check. Anything else: the event is dropped."""
    if not isinstance(ev, dict) or ev.get("event") not in TELE_SCHEMA or not isinstance(ev.get("props"), dict):
        return False
    spec, props = TELE_SCHEMA[ev["event"]], ev["props"]
    if set(ev) - {"event", "props"} or any(k not in props for k in TELE_REQUIRED[ev["event"]]):
        return False
    return all(k in spec and spec[k](v) for k, v in props.items())


def bw_home():
    d = os.environ.get("BW_HOME") or os.path.join(os.path.expanduser("~"), ".config", "bw")
    os.makedirs(d, exist_ok=True)
    return d


class _Lock:
    """An exclusive lock on bw's own folder: hooks run in parallel and all add to the same day counts."""
    def __enter__(self):
        self.f = open(os.path.join(bw_home(), ".lock"), "a")
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, *a):
        self.f.close()


def tele_conf():
    return load(os.path.join(bw_home(), "telemetry.json"), {}) or {}


def tele_blocked():
    """Hard off switches, whatever the person chose: DO_NOT_TRACK, BW_TELEMETRY=0, CI."""
    if os.environ.get("DO_NOT_TRACK", "") not in ("", "0") or os.environ.get("BW_TELEMETRY", "") == "0":
        return "DO_NOT_TRACK or BW_TELEMETRY=0 is set"
    if os.environ.get("CI"):
        return "running in CI"
    return None


def tele_on():
    return bool(tele_conf().get("on")) and not tele_blocked()


def tele_repo_id(bwd):
    """A random id for this repo, made the first time bw counts here. Not derived from the path or the remote."""
    p = os.path.join(bwd, "tele_repo")
    try:
        rid = open(p).read().strip()
        if _RX_REPO.match(rid):
            return rid
    except Exception:
        pass
    rid = "r_" + uuid.uuid4().hex[:8]
    try:
        with open(p, "w") as f:
            f.write(rid)
    except Exception:
        pass
    return rid


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _day_path():
    return os.path.join(bw_home(), "days.json")


def tele_count(bwd, counters=None, **groups):
    """Add to today's counts for this repo, locally. groups: decisions/cmds/hooks/files/receipt_time -> {name: n}."""
    try:
        rid = tele_repo_id(bwd)
        with _Lock():
            days = load(_day_path(), {}) or {}
            row = days.setdefault(_today(), {}).setdefault(rid, {})
            for k, n in (counters or {}).items():
                if k in TELE_COUNTERS and n:
                    row[k] = row.get(k, 0) + int(n)
            for g, vals in groups.items():
                for k, n in (vals or {}).items():
                    if n:
                        row.setdefault(g, {})[k] = row.get(g, {}).get(k, 0) + int(n)
            save(_day_path(), days)
    except Exception:
        pass


def _cause(e):
    import errno as _errno
    msg = str(e)
    gitcmd = None
    if isinstance(e, RuntimeError) and msg.startswith("git "):
        sub = msg[4:].split(" ", 1)[0].rstrip(":")
        gitcmd = sub if sub in TELE_GITCMDS else "other"
        return ("git_unsafe_repo" if "dubious ownership" in msg else "git_failed"), gitcmd
    if "dubious ownership" in msg:
        return "git_unsafe_repo", None
    if isinstance(e, FileNotFoundError) and getattr(e, "filename", None) == "git":
        return "git_missing", None
    if isinstance(e, json.JSONDecodeError):
        return "state_corrupt", None
    if isinstance(e, subprocess.TimeoutExpired):
        return "subprocess_timeout", None
    if isinstance(e, OSError) and e.errno == _errno.EADDRINUSE:
        return "server_port", None
    if isinstance(e, OSError) and e.errno in (_errno.EIO, _errno.ENOSPC, _errno.EACCES, _errno.EROFS, _errno.EPERM, _errno.EMFILE, _errno.EDQUOT):
        return "io_error", None
    return "other", None


def _exc_type(e):
    import builtins
    for c in type(e).__mro__:
        if getattr(builtins, c.__name__, None) is c:
            return c.__name__
    return "Other"


def _redact(text):
    """For the local log shown by /bw:doctor <id>: home folder and repo paths replaced, so pasting it is safer."""
    home = os.path.expanduser("~")
    text = re.sub(r"(/[\w.@+-]+){2,}", lambda m: "<HOME>" + m.group(0)[len(home):] if m.group(0).startswith(home) else "<PATH>", text)
    return text


def tele_error(stage, e, bwd=None, tb=None):
    """Record a bw failure: the full message and traceback in the local errors.log under a short id; a fingerprint
    (bw function, bw line, error type, cause code) in today's counts. Returns the id."""
    import traceback
    eid = "e" + uuid.uuid4().hex[:4]
    try:
        tb = tb if tb is not None else e.__traceback__
        frames = [f for f in traceback.extract_tb(tb) if os.path.abspath(f.filename) == os.path.abspath(__file__)] if tb else []
        fn, line = (frames[-1].name, frames[-1].lineno) if frames else ("module", 0)
        if fn not in ("module", "nested") and not _v_fn(fn):
            fn = "nested" if fn != "<module>" else "module"
        cause, gitcmd = _cause(e)
        fields = {"v": BW_VERSION, "stage": stage if stage in TELE_STAGES else "send", "fn": fn, "line": line, "type": _exc_type(e), "cause": cause,
                  "errno": __import__("errno").errorcode.get(getattr(e, "errno", None) or -1, "none")}
        if gitcmd:
            fields["gitcmd"] = gitcmd
        with open(os.path.join(bw_home(), "errors.log"), "a") as f:
            f.write("%s %s %s %s:%d %s %s\n%s\n" % (eid, time.strftime("%Y-%m-%d %H:%M:%S"), stage, fn, line, cause, repr(e)[:500],
                    "".join(traceback.format_tb(tb))[-3000:] if tb else ""))
        _tele_err_count(bwd, fields, eid)
    except Exception:
        pass
    return eid


def _tele_err_count(bwd, fields, eid):
    rid = tele_repo_id(bwd) if bwd else None
    fp = ":".join(str(fields.get(k, "")) for k in ("stage", "fn", "line", "type", "cause"))
    with _Lock():
        days = load(_day_path(), {}) or {}
        e = days.setdefault(_today(), {}).setdefault("_errors", {}).setdefault(fp, dict(fields, count=0, err_ids=[]))
        if rid:
            e["repo"] = rid
        e["count"] += 1
        if len(e["err_ids"]) < 5:
            e["err_ids"].append(eid)
        save(_day_path(), days)


# Hook marks: Claude Code kills a hook that runs past its timeout, and bw never hears about it. Each hook leaves a mark
# when it starts and removes it when it ends; a mark left behind by a process that no longer runs is a timeout.
def _marks():
    d = os.path.join(bw_home(), "running")
    os.makedirs(d, exist_ok=True)
    return d


def hook_mark_start(hook, bwd):
    p = os.path.join(_marks(), "%s.%d.%d.%s" % (hook, os.getpid(), int(time.time()), tele_repo_id(bwd)))
    try:
        open(p, "w").close()
    except Exception:
        return None
    for name in os.listdir(_marks()):
        try:
            h, pid, t, rid = name.split(".")
            if int(pid) == os.getpid() or time.time() - int(t) < HOOK_TIMEOUT.get(h, 60) + 15:
                continue
            try:
                os.kill(int(pid), 0)
                continue  # still running
            except ProcessLookupError:
                pass
            except Exception:
                continue
            os.remove(os.path.join(_marks(), name))
            eid = "e" + uuid.uuid4().hex[:4]
            with open(os.path.join(bw_home(), "errors.log"), "a") as f:
                f.write("%s %s %s module:0 hook_timeout hook did not finish (killed at its %ds timeout?)\n" % (eid, time.strftime("%Y-%m-%d %H:%M:%S"), h, HOOK_TIMEOUT.get(h, 60)))
            fields = {"v": BW_VERSION, "stage": h if h in TELE_HOOKS else "send", "fn": "module", "line": 0, "type": "Other", "cause": "hook_timeout", "errno": "none"}
            if _RX_REPO.match(rid):
                fields["repo"] = rid
            _tele_err_count(None, fields, eid)
        except Exception:
            continue
    return p


def hook_mark_end(p):
    try:
        if p:
            os.remove(p)
    except Exception:
        pass


def _bucket(n, edges, names):
    for e, name in zip(edges, names):
        if n <= e:
            return name
    return names[-1]


def tele_stop(bwd, task, files, rc, bel, data, last=""):
    """Counts from one finished task: what the receipt showed and whether bw's probe request was answered."""
    rows = [r for r in (rc or {}).get("rows", [])]
    c = {"tasks": 1, "receipts": 1 if rows else 0, "probes_run": sum(1 for r in rows if "after" in r),
         "probe_skipped": sum(1 for r in rows if r.get("skipped"))}
    for r in rows:
        v = r.get("verdict")
        if v in ("changed", "same", "cant_tell", "flaky"):
            c[v] = c.get(v, 0) + 1
        why = r.get("why") or ""
        if "not found" in why:
            c["probe_cmd_missing"] = c.get("probe_cmd_missing", 0) + 1
        elif "timed out" in why:
            c["probe_timeout"] = c.get("probe_timeout", 0) + 1
    if task.get("probe_ask"):
        c["probe_ask"] = 1
        c["probe_ask_answered"] = 1 if any(r.get("changed") for r in rows) else 0
        c["no_probe_reply"] = 1 if data.get("noprobe") and no_probe_reason(last or "") else 0
    if data.get("seed"):
        c["first_probe_ask"] = 1
    if bel:
        c["beliefs_run"] = bel.get("checked", 0)
        c["beliefs_broke"] = len(bel.get("broken", []))
    groups = {"files": {_bucket(len(files), (0, 3, 10), TELE_FILES): 1}}
    if rc and rows:
        groups["receipt_time"] = {_bucket(rc.get("secs", 0), (10, 60, 180), TELE_RTIME): 1}
    tele_count(bwd, c, **groups)


DECISION_TELE = (("fix", "fix"), ("expected", "expected"), ("skip", "skip"), ("kept", "keep"), ("keep", "keep"),
                 ("looks fixed", "keep"), ("not intended", "not_intended"), ("open", "leave_open"))


def tele_use(bwd, ev, kw):
    """The local use events that also become day counts (names and ids in kw are never copied)."""
    if ev == "review":
        tele_count(bwd, {"tasks": 1, "reviews": 1, "findings_shown": kw.get("shown", 0), "findings_unshown": kw.get("unshown", 0)})
    elif ev == "decision":
        d = str(kw.get("decision") if not isinstance(kw.get("decision"), dict) else kw["decision"].get("label", "")).lower()
        name = next((t for k, t in DECISION_TELE if k in d), "other")
        tele_count(bwd, decisions={name: 1})
    elif ev == "predicted":
        tele_count(bwd, decisions={"predicted": 1})
    elif ev == "opened":
        tele_count(bwd, {"pages": 1})
    elif ev == "plan-opened":
        tele_count(bwd, {"plan_opened": 1})
    elif ev == "plan-pass":
        tele_count(bwd, {"plan_approved": 1})
    elif ev == "plan-rev":
        tele_count(bwd, {"plan_changes": 1})


def _vers():
    def v(cmd, rx):
        try:
            m = re.search(rx, subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)
            return m.group(1) if m else None
        except Exception:
            return None
    osf = "macos" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else "other"
    if osf == "linux":
        try:
            if "microsoft" in open("/proc/version").read().lower():
                osf = "wsl"
        except Exception:
            pass
    return {"v": BW_VERSION, "py": "%d.%d" % sys.version_info[:2], "git": v(["git", "--version"], r"(\d+\.\d+)"),
            "cc": v(["claude", "--version"], r"(\d+\.\d+\.\d+)"), "os": osf}


def tele_pending():
    """The events the next send would carry: every finished day (not today), plus an install event on a new version."""
    days = load(_day_path(), {}) or {}
    conf = tele_conf()
    evs = []
    if conf.get("sent_version") != BW_VERSION:
        evs.append({"event": "install", "props": {k: v for k, v in _vers().items() if v}})
    for date in sorted(days):
        if date >= _today():
            continue
        for rid, row in days[date].items():
            if rid == "_errors":
                for e in row.values():
                    evs.append({"event": "error", "props": dict({"v": BW_VERSION}, **dict(e, date=date))})
            else:
                evs.append({"event": "day", "props": dict(row, date=date, repo=rid, v=BW_VERSION)})
    return evs


def tele_send(force=False):
    """Send finished days if the person turned telemetry on. Every event is checked against the schema first; an event
    that fails is dropped and never retried. On success the days leave days.json and are kept in sent.jsonl for 30 days."""
    if not tele_on() or not TELE_KEY:
        return "off" if not tele_on() else "no telemetry project configured"
    conf = tele_conf()
    if not force and time.time() - conf.get("last_send", 0) < TELE_SEND_EVERY:
        return "sent less than a day ago"
    evs = [e for e in tele_pending() if tele_valid(e)]
    if not evs:
        return "nothing to send"
    iid = conf.get("install_id")
    batch = [{"event": "bw_" + e["event"], "distinct_id": iid,
              "timestamp": (e["props"].get("date") or _today()) + "T12:00:00Z",
              "properties": dict(e["props"], **{"$process_person_profile": False, "$geoip_disable": True, "$lib": "bw"})} for e in evs]
    import urllib.request
    req = urllib.request.Request(TELE_HOST.rstrip("/") + "/batch/", data=json.dumps({"api_key": TELE_KEY, "batch": batch}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            ok = 200 <= r.status < 300
    except Exception as e:
        tele_error("send", e)
        return "send failed (kept for next time)"
    if not ok:
        return "send refused (kept for next time)"
    with _Lock():
        days = load(_day_path(), {}) or {}
        for d in [d for d in days if d < _today()]:
            days.pop(d)
        save(_day_path(), days)
        conf = tele_conf()
        conf.update(last_send=time.time(), sent_version=BW_VERSION)
        save(os.path.join(bw_home(), "telemetry.json"), conf)
    sp = os.path.join(bw_home(), "sent.jsonl")
    keep = []
    try:
        keep = [l for l in open(sp) if json.loads(l).get("at", 0) > time.time() - 30 * 86400]
    except Exception:
        pass
    with open(sp, "w") as f:
        f.writelines(keep)
        for e in evs:
            f.write(json.dumps(dict(e, at=round(time.time()))) + "\n")
    return "sent %d event%s" % (len(evs), "" if len(evs) == 1 else "s")


def tele_maybe_send():
    """From the Stop hook: start a detached send when one is due. Never waits for the network."""
    try:
        if tele_on() and TELE_KEY and time.time() - tele_conf().get("last_send", 0) >= TELE_SEND_EVERY \
                and any(d < _today() for d in (load(_day_path(), {}) or {})):
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "telemetry", "send"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


TELE_SAYS = """bw telemetry is {state}.
  When on, bw sends once a day: counts of tasks, receipts, probe results, decisions, pages and bw commands; bw, Claude Code,
  Python and git versions and your OS family; and bw's own error codes (bw function, bw line, error type). Never paths,
  file or folder names, repo names, code, probe output, prompts, finding text, names or emails. Ids are random.
  See exactly what would be sent: /bw:telemetry show   Already sent: /bw:telemetry log   Local folder: {home}"""


def cmd_telemetry(args):
    """bwcap.py telemetry [on|off|show|log|send]"""
    sub = args[0] if args else "status"
    path = os.path.join(bw_home(), "telemetry.json")
    if sub in ("on", "off"):
        with _Lock():
            conf = tele_conf()
            conf["on"] = sub == "on"
            conf["asked"] = time.time()
            if sub == "on" and not conf.get("install_id"):
                conf["install_id"] = "i_" + uuid.uuid4().hex[:16]
            save(path, conf)
        blocked = tele_blocked()
        print("bw telemetry is %s.%s" % (sub, (" (but nothing is sent here: %s)" % blocked) if sub == "on" and blocked else ""))
        if sub == "on":
            print("Thank you. See what is sent with /bw:telemetry show; turn off any time with /bw:telemetry off.")
        return
    if sub == "show":
        evs = tele_pending()
        good = [e for e in evs if tele_valid(e)]
        print("bw telemetry is %s. Next send would carry %d event%s%s:" % ("on" if tele_on() else "off", len(good), "" if len(good) == 1 else "s",
              (" (%d more dropped by the allowlist)" % (len(evs) - len(good))) if len(evs) != len(good) else ""))
        for e in good:
            print(json.dumps(e, sort_keys=True))
        today = (load(_day_path(), {}) or {}).get(_today())
        if today:
            print("Today, counted so far (sent after today ends):")
            print(json.dumps(today, sort_keys=True))
        return
    if sub == "log":
        try:
            print(open(os.path.join(bw_home(), "sent.jsonl")).read().strip() or "Nothing sent yet.")
        except Exception:
            print("Nothing sent yet.")
        return
    if sub == "send":
        print(tele_send(force="--force" in args))
        return
    print(TELE_SAYS.format(state="on" if tele_on() else ("off (%s)" % tele_blocked() if tele_conf().get("on") else "off"), home=bw_home()))


def cmd_onboard(args):
    """bwcap.py onboard scan | verify"""
    root = repo_root(os.getcwd())
    if not root:
        print("bw: this folder is not inside a git repository"); return 1
    if not enabled(root):
        cmd_on([root])
        print("")
    bwd = bw_dir(root)
    sub = args[0] if args else "scan"
    if sub == "scan":
        scan = onboard_scan(root, bwd)
        tele_count(bwd, {"onboard_scans": 1})
        print(onboard_worklist(scan, os.path.abspath(__file__)))
        return 0
    if sub == "verify":
        res = onboard_verify(root, bwd)
        print("bw onboarding: %d probe%s run and stay stable, %d not used (%.0f s)." % (res["ok"], "" if res["ok"] == 1 else "s", res["failed"], res["secs"]))
        for r in res["rows"]:
            if r["problems"]:
                print("  not used: %s - %s" % (r["probe"], "; ".join(r["problems"])))
            elif r.get("hollow") and not r["hollow"][0]:
                print("  weak: %s - none of %d planted breaks changed its output. Breaks it missed:" % (r["probe"], r["hollow"][1]))
                for m in r.get("missed") or []:
                    print("      " + m)
        info = server_info(bwd)
        url = home_url(root, bwd)
        print("Onboarding page: %s" % (url.replace("home.html", "onboard.html") if url.startswith("http") else "file://" + os.path.join(bwd, "onboard.html")))
        return 0
    print("usage: bwcap.py onboard scan | verify"); return 2


def cmd_doctor(args):
    """Read-only setup check, or one local error by id: bwcap.py doctor [e1a2]"""
    if args and _RX_ERRID.match(args[0]):
        try:
            text = open(os.path.join(bw_home(), "errors.log")).read()
        except Exception:
            text = ""
        parts = re.split(r"(?m)^(?=e[0-9a-f]{4} \d{4}-)", text)
        hit = [p for p in parts if p.startswith(args[0] + " ")]
        if not hit:
            print("No bw error with id %s on this machine." % args[0])
            return
        print("bw error %s, with your home folder and other paths replaced. Read it before sharing:\n" % args[0])
        print(_redact(hit[-1]).strip())
        return
    ok = lambda b: "ok  " if b else "FIX "
    py = sys.version_info >= (3, 8)
    print("%s python %d.%d%s" % (ok(py), sys.version_info[0], sys.version_info[1], "" if py else " (bw needs 3.8 or newer)"))
    gv = _vers()
    print("%s git %s" % (ok(gv["git"]), gv["git"] or "not found on PATH"))
    print("ok   bw %s, %s, Claude Code %s" % (BW_VERSION, gv["os"], gv["cc"] or "not found on PATH"))
    root = repo_root(args[0] if args else os.getcwd())
    if not root:
        print("--   this folder is not in a git repository")
    else:
        on = enabled(root)
        print("%s bw is %s in this repo%s" % (ok(on), "on" if on else "off", "" if on else " (turn on: /bw:on)"))
        if on:
            bwd = bw_dir(root)
            np = len(load_probes(root, "HEAD"))
            print("ok   %d probe%s committed at HEAD" % (np, "" if np == 1 else "s"))
            days = load(_day_path(), {}) or {}
            rid = tele_repo_id(bwd)
            hooks = {}
            for d in sorted(days)[-7:]:
                for h, n in (days[d].get(rid, {}).get("hooks") or {}).items():
                    hooks[h] = hooks.get(h, 0) + n
            print("%s hooks seen in the last 7 days: %s" % (ok(hooks.get("prompt")), ", ".join("%s %d" % kv for kv in sorted(hooks.items()))
                  or "none (restart Claude Code in this repo after installing)"))
    try:
        lines = [l for l in open(os.path.join(bw_home(), "errors.log")) if re.match(r"^e[0-9a-f]{4} \d{4}-", l)]
    except Exception:
        lines = []
    if lines:
        print("--   last bw errors (see one: /bw:doctor <id>):")
        for l in lines[-3:]:
            f = l.split(" ")
            print("       %s %s %s %s %s" % (f[0], f[1], f[3], f[4], f[5] if len(f) > 5 else ""))
    else:
        print("ok   no bw errors recorded on this machine")
    print("--   telemetry %s (/bw:telemetry)" % ("on" if tele_on() else "off"))


def main():
    name = sys.argv[1] if len(sys.argv) >= 2 else ""
    if name == "telemetry":
        return cmd_telemetry(sys.argv[2:])
    if name == "doctor":
        try:
            r = repo_root(os.getcwd())
            if r and enabled(r):
                tele_count(bw_dir(r), cmds={"doctor": 1})
        except Exception:
            pass
        return cmd_doctor(sys.argv[2:])
    if name == "hook" or name not in TELE_CMDS:
        return _dispatch()
    bwd = None
    try:
        r = repo_root(os.getcwd())
        if r and enabled(r) and name != "serve":
            bwd = bw_dir(r)
            tele_count(bwd, cmds={name: 1})
    except Exception:
        pass
    try:
        return _dispatch()
    except Exception as e:
        tele_error("cmd-" + name, e, bwd)
        raise


def _dispatch():
    cli = {"receipt": cmd_receipt, "beliefs": cmd_beliefs, "keep": cmd_keep, "agents-md": cmd_agents_md, "task": cmd_task,
           "how": cmd_how, "why": cmd_why, "recall": cmd_recall, "brief": cmd_brief, "findings": cmd_findings, "hollow": cmd_hollow,
           "onboard": cmd_onboard}
    if len(sys.argv) >= 2 and sys.argv[1] in cli:
        sys.exit(cli[sys.argv[1]](sys.argv[2:]) or 0)
    if len(sys.argv) >= 2 and sys.argv[1] == "do":
        cmd_do(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "plan":
        cmd_plan(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        cmd_serve(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "stats":
        cmd_stats(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "map":
        cmd_map(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] in ("on", "off"):
        (cmd_on if sys.argv[1] == "on" else cmd_off)(sys.argv[2:])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "hook":
        try:
            inp = json.load(sys.stdin)
        except Exception:
            inp = {}
        if os.environ.get("BW_DEBUG", "0") == "1":  # trial only: record which fields the hooks really receive
            try:
                root = hook_root(inp)
                if root:
                    with open(os.path.join(bw_dir(root), "payloads.log"), "a") as f:
                        f.write(json.dumps({"event": sys.argv[2], "keys": sorted(inp.keys())}) + "\n")
            except Exception:
                pass
        hb, mark = None, None
        try:  # count the hook and leave a mark that a timeout would leave behind; only in repos where bw is on
            r0 = hook_root(dict(inp))
            if r0 and enabled(r0) and sys.argv[2] in TELE_HOOKS:
                hb = bw_dir(r0)
                mark = hook_mark_start(sys.argv[2], hb)
                tele_count(hb, hooks={sys.argv[2]: 1})
        except Exception:
            pass
        try:
            out = {"prompt": hook_prompt, "pre": hook_pre, "post": hook_post, "stop": hook_stop_all,
                   "plan": hook_plan, "allow": hook_allow, "postfail": hook_postfail}[sys.argv[2]](inp)
            if out:
                print(json.dumps(out))
        except Exception as e:  # a hook must never break the agent's session
            tele_error(sys.argv[2], e, hb)
        hook_mark_end(mark)
        if sys.argv[2] == "stop" and hb:
            tele_maybe_send()
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "home":
        # open bw home for the repo in the current directory
        root = repo_root(os.getcwd())
        if not root or not enabled(root):
            print("bw is not on in this folder. Type /bw:on to turn it on.")
            return
        bwd = bw_dir(root)
        render_home(bwd, load_map(root, bwd), root)
        url = home_url(root, bwd) + "#catchup"
        open_page(url)
        print(url)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        # past commits as past tasks (no cards), so the home map starts with real history: bwcap.py backfill [N]
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        root = repo_root(os.getcwd())
        bwd = bw_dir(root)
        m = load_map(root, bwd)
        backfill(root, bwd, m, n)
        print(render_home(bwd, m, root))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "render":
        # re-render a finished task: bwcap.py render <task-dir>
        d = sys.argv[2]
        task = load(os.path.join(d, "task.json"), None)
        m = load_map(task["root"], bw_dir(task["root"]))
        files = changed_files(task["root"], task["start"], task["end"])
        data = build_data(task, m, files, load(os.path.join(d, "cards.json"), {"cards": []}),
                          last_agent_text(task.get("transcript"), task["started"]))
        print(render(data, os.path.join(d, "debrief.html")))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
