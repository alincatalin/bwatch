import importlib.util, os
os.environ.pop("BW_REPO", None)
spec = importlib.util.spec_from_file_location("bwcap", "plugin/scripts/bwcap.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
long_a = "\n".join("row %d: ok" % i for i in range(80))
long_b = long_a.replace("row 70: ok", "row 70: WRONG")
cases = [
    ("plain value 10 -> 12", "total: 10", "total: 12"),
    ("due date Nov 1 -> Dec 1", "due: 2026-11-01T00:00:00Z", "due: 2026-12-01T09:00:00Z"),
    ("retry delay thirty->five s", "retry after 30s", "retry after 5s"),
    ("timeout 500->5 millis", "timeout 500 ms", "timeout 5 ms"),
    ("line 71 of 80 changes", long_a, long_b),
]
for label, before, after in cases:
    tree, root = "/work/.git/bw/wt/0123abcd", "/work"
    same = b.normalize_out(before, tree, root) == b.normalize_out(after, tree, root)
    print("%-26s -> receipt says %s" % (label, "SAME" if same else "changed"))
