import json, os, re, shutil, subprocess, sys, tempfile
os.environ.pop("BW_REPO", None)
sys.path.insert(0, "plugin/scripts")
import bwcap
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
subjects = [("plain subject", "Fix rounding"), ("ampersand", "A & B"), ("html tag", "<img src=x onerror=alert(1)>")]
tmp = tempfile.mkdtemp()
try:
    run = lambda *a: subprocess.run(["git"] + list(a), cwd=tmp, env=env, capture_output=True, check=True)
    run("init", "-q")
    open(os.path.join(tmp, "a.py"), "w").write("x=0\n")
    run("add", "."); run("commit", "-qm", "init")
    for i, (_, s) in enumerate(subjects):
        open(os.path.join(tmp, "a.py"), "w").write("x=%d\n" % (i + 1))
        run("commit", "-qam", s)
    bwd = bwcap.bw_dir(tmp)
    m = {"regions": []}
    bwcap.backfill(tmp, bwd, m, 10)
    page = open(bwcap.render_home(bwd, m, tmp)).read()
    data = json.loads(re.search(r"var D=(.*?);\n", page).group(1).replace("<\\/", "</"))
    titles = [t["title"] for t in data["tasks"]]
    for label, s in subjects:
        got = [t for t in titles if t in (s, s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))]
        shown = got[0] if got else "(missing)"
        print("%-14s -> page gets %r -> %s" % (label, shown, "runs as HTML" if "<" in shown else "shown as text"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
