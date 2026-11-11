import importlib.util, json, os, subprocess, tempfile, shutil
for k in ("BW_REPO", "BW_PAGE"):
    os.environ.pop(k, None)
os.environ.update(BW_OPEN="0", BW_ASK_PROBES="0")
spec = importlib.util.spec_from_file_location("bwcap", "plugin/scripts/bwcap.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.home_url = lambda *a: "http://local"  # no page server in a probe
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
os.environ.update({k: env[k] for k in env if k.startswith("GIT_")})
tmp = tempfile.mkdtemp()
try:
    run = lambda *a: subprocess.run(["git"] + list(a), cwd=tmp, capture_output=True, check=True)
    run("init", "-q"); os.makedirs(os.path.join(tmp, "src"))
    open(os.path.join(tmp, "src", "a.py"), "w").write("x=1\n"); run("add", "."); run("commit", "-qm", "init")
    bwd = b.bw_dir(tmp); open(os.path.join(bwd, "enabled"), "w").write("on\n")
    hk = lambda **kw: dict({"session_id": "s1", "cwd": tmp}, **kw)
    b.hook_prompt(hk(prompt="change a"))
    open(os.path.join(tmp, "src", "a.py"), "w").write("x=2\n")
    real = b.changed_files
    b.changed_files = lambda *a: (_ for _ in ()).throw(RuntimeError("stop hook died (timeout / EIO)"))
    try:
        b.hook_stop(hk())
        print("first stop: finished")
    except RuntimeError:
        print("first stop: crashed after the end snapshot")
    b.changed_files = real
    b.hook_prompt(hk(prompt="now add b"))
    open(os.path.join(tmp, "src", "b.py"), "w").write("y=1\n")
    b.hook_stop(hk())
    tid = sorted(os.listdir(os.path.join(bwd, "tasks")))[0]
    t = json.load(open(os.path.join(bwd, "tasks", tid, "task.json")))
    d = json.load(open(os.path.join(bwd, "tasks", tid, "data.json")))
    files = sorted({p for r in d["regions"] for p in r["paths"]} | set(d["unmapped"]))
    print("tasks recorded:", len(os.listdir(os.path.join(bwd, "tasks"))))
    print("prompts in that task:", t["prompts"])
    print("files in its debrief:", files)
    print("src/b.py seen by bw:", "src/b.py" in files)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
