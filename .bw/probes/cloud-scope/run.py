import os, sys; sys.path.insert(0, "plugin/scripts")
os.environ["BW_REPO"] = "/work/shared/app"
import bwcap
root = "/work/shared/app"
cases = [("git status inside the repo", "git status", "/work/shared/app"),
         ("run from a subfolder", "pytest -q", "/work/shared/app/src"),
         ("names the repo by path", "python3 /work/shared/app/tool.py receipt", "/home/user"),
         ("uses $BW_REPO", "git -C $BW_REPO log -1", "/home/user"),
         ("reads notes elsewhere", "cat /home/user/notes.md", "/home/user"),
         ("clones into scratch", "git clone /srv/other scratch/x", "/home/user")]
for name, cmd, cwd in cases:
    print("%-28s -> %s" % (name, "logged" if bwcap.about_repo(root, cmd, cwd) else "not logged"))
