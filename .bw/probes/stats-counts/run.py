import contextlib, io, json, os, subprocess, sys, tempfile; sys.path.insert(0, "plugin/scripts")
import bwcap
with tempfile.TemporaryDirectory() as d:
    subprocess.run(["git", "init", "-q"], cwd=d)
    bwd = bwcap.bw_dir(d)
    open(os.path.join(bwd, "enabled"), "w").write("on\n")
    at = 1794398400  # 11 Nov 2026, fixed
    ev = [dict(ev="stop", at=at, task="t1", run="t1", items=1, notest=0),
          dict(ev="review", at=at + 60, task="t2", run="t2", shown=5, unshown=3, cant=0),
          dict(ev="review", at=at + 120, task="t3", shown=0, unshown=0, cant=0)]
    open(os.path.join(bwd, "use.jsonl"), "w").write("".join(json.dumps(e) + "\n" for e in ev))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bwcap.cmd_stats([d])
    total = [l for l in buf.getvalue().splitlines() if l.startswith("total")][0].split()
    print("runs:", total[1], "| for you:", total[2], "| decisions:", total[6])
