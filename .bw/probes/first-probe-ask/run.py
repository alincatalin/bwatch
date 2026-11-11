import json, os, subprocess, sys, tempfile; sys.path.insert(0, "plugin/scripts")
os.environ.pop("BW_REPO", None); os.environ["BW_OPEN"] = "0"
import bwcap
bwcap.home_url = lambda root, bwd: "http://watch-room"
bwcap.open_page = lambda url: None

def run(name, change, with_probe, cls=None):
    with tempfile.TemporaryDirectory() as d:
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, capture_output=True)
        os.makedirs(os.path.join(d, "app"))
        open(os.path.join(d, "app", "fees.py"), "w").write("def fee(n):\n    return n * 2\n")
        open(os.path.join(d, "README.md"), "w").write("app\n")
        if with_probe:
            os.makedirs(os.path.join(d, ".bw", "probes", "fee"))
            open(os.path.join(d, ".bw", "probes", "fee", "probe.json"), "w").write('{"scenario": "fee of 3", "cmd": "python3 -c \\"import sys; sys.path.insert(0, \'app\'); import fees; print(fees.fee(3))\\""}')
        g("init", "-q"); g("add", "-A"); g("commit", "-qm", "init")
        bwd = bwcap.bw_dir(d)
        open(os.path.join(bwd, "enabled"), "w").write("on\n")
        m = bwcap.draft_map(d)
        for r in m["regions"]:
            if cls and r.get("layer") == "code":
                r["class"] = cls
        bwcap.save(os.path.join(bwd, "map.json"), m)
        inp = {"session_id": "s1", "cwd": d}
        bwcap.hook_prompt(dict(inp, prompt="change the fee"))
        path, text = change
        open(os.path.join(d, path), "w").write(text)
        out = bwcap.hook_stop(dict(inp, last_assistant_message="done")) or {}
        if out.get("decision") == "block":
            print("%-34s -> asks: %s" % (name, out["reason"].splitlines()[0]))
        else:
            print("%-34s -> no ask (%s)" % (name, (out.get("systemMessage") or "silent").split("(")[0].strip()))

run("code changed, no probe in repo", ("app/fees.py", "def fee(n):\n    return n * 3\n"), False)
run("code in a region marked fix, no probe", ("app/fees.py", "def fee(n):\n    return n * 3\n"), False, "repair")
run("only README changed, no probe", ("README.md", "app v2\n"), False)
run("code changed, repo has a probe", ("app/fees.py", "def fee(n):\n    return n * 3\n"), True)
