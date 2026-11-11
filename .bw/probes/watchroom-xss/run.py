import importlib.util, os, subprocess, tempfile, shutil
os.environ.pop("BW_REPO", None)
spec = importlib.util.spec_from_file_location("bwcap", "plugin/scripts/bwcap.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
tpl = open("plugin/scripts/watch_template.html").read()
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
    bwd = b.bw_dir(tmp)
    b.backfill(tmp, bwd, {"regions": []}, 10)
    titles = [t["title"] for t in b.watch_data(bwd, {"regions": []}, tmp)["tasks"]]
    for label, s in subjects:
        print("%-14s -> title sent to the page: %r" % (label, s if s in titles else "(escaped/missing)"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("template inserts t.title unescaped into innerHTML:", "<div class=\"tt\">'+t.title+'</div>" in tpl)
