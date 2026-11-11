import os, subprocess, sys, tempfile; sys.path.insert(0, "plugin/scripts")
import bwcap
layouts = {
    "code only in plugin/scripts": ["plugin/scripts/tool.py", "plugin/scripts/page.html", "README.md", "tests/test_tool.py"],
    "src next to scripts": ["src/orders/api.py", "src/orders/model.py", "src/billing/invoice.py", "scripts/deploy.py", "README.md"],
    "go service": ["cmd/server/main.go", "internal/auth/token.go", "internal/store/db.go", "internal/store/db_test.go", "go.mod"],
}
for name, files in layouts.items():
    with tempfile.TemporaryDirectory() as d:
        for f in files:
            os.makedirs(os.path.join(d, os.path.dirname(f)), exist_ok=True)
            open(os.path.join(d, f), "w").write("x = 1\n")
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, capture_output=True)
        g("init", "-q"); g("add", "-A"); g("commit", "-qm", "init")
        m = bwcap.draft_map(d)
        code = ["%s [%s] %s" % (r["name"], bwcap.CLASS_SAY.get(r["class"], r["class"]), ",".join(r["paths"])) for r in m["regions"] if r.get("layer") == "code"]
        print("%s:\n  %s" % (name, "\n  ".join(code) or "NO CODE REGION"))
