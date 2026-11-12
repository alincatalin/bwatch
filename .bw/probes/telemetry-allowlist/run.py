import json, os, sys, tempfile; sys.path.insert(0, "plugin/scripts")
os.environ["BW_HOME"] = tempfile.mkdtemp()
import bwcap
day = {"date": "2026-11-11", "repo": "r_0123abcd", "v": "0.14.0"}
cases = [
    ("day: counts only", {"event": "day", "props": dict(day, tasks=3, changed=1, cmds={"how": 1})}),
    ("day: unknown field 'prompt'", {"event": "day", "props": dict(day, prompt="fix the login bug")}),
    ("day: a count given as text", {"event": "day", "props": dict(day, tasks="3")}),
    ("day: a command not on the list", {"event": "day", "props": dict(day, cmds={"src/billing.py": 1})}),
    ("day: repo id that is a path", {"event": "day", "props": dict(day, repo="/home/ann/acme")}),
    ("error: bw function and line", {"event": "error", "props": {"date": "2026-11-11", "v": "0.14.0", "stage": "stop", "fn": "receipt",
                                     "line": 1665, "type": "OSError", "errno": "EIO", "cause": "io_error", "count": 2}}),
    ("error: a function not in bw", {"event": "error", "props": {"date": "2026-11-11", "v": "0.14.0", "stage": "stop", "fn": "charge_card",
                                     "line": 3, "type": "OSError", "cause": "io_error", "count": 1}}),
    ("error: message text as type", {"event": "error", "props": {"date": "2026-11-11", "v": "0.14.0", "stage": "stop", "fn": "receipt",
                                     "line": 3, "type": "No such file: /Users/ann/acme/x.py", "cause": "io_error", "count": 1}}),
    ("install: versions and os", {"event": "install", "props": {"v": "0.14.0", "py": "3.12", "git": "2.47", "cc": "2.1.322", "os": "macos"}}),
    ("install: hostname added", {"event": "install", "props": {"v": "0.14.0", "os": "macos", "host": "anns-macbook"}}),
    ("extra top-level key", {"event": "day", "props": day, "path": "/home/ann"}),
]
for name, ev in cases:
    print("%-34s %s" % (name, "sent" if bwcap.tele_valid(ev) else "dropped"))
# a real failure: the message and path stay in the local log, the event carries only the fingerprint
try:
    open("/home/ann/acme/secret-plan.txt")
except Exception as e:
    bwcap.tele_error("stop", e)
ev = [e for e in bwcap.tele_pending() if e["event"] == "error"]
days = json.load(open(os.path.join(os.environ["BW_HOME"], "days.json")))
d = sorted(days)[0]
props = list(days[d]["_errors"].values())[0]
print("recorded error fields:", ", ".join(sorted(k for k in props if k != "err_ids")))
print("path in recorded event:", "acme" in json.dumps(props), "| path in local log:", "acme" in open(os.path.join(os.environ["BW_HOME"], "errors.log")).read())
