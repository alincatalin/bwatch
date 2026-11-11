import importlib.util, json, os, tempfile, shutil
os.environ.pop("BW_REPO", None)
spec = importlib.util.spec_from_file_location("bwcap", "plugin/scripts/bwcap.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
good = {"beliefs": [{"id": "bl-1", "person": "me", "state": "holds", "probe": {"id": "p1", "cmd": "true"}, "expect": "x"},
                    {"id": "bl-2", "person": "me", "state": "holds", "probe": {"id": "p2", "cmd": "true"}, "expect": "y"}]}
full = json.dumps(good)
cases = [("intact file", full), ("cut short mid-write", full[: len(full) // 2]), ("empty file", "")]
tmp = tempfile.mkdtemp()
try:
    for label, text in cases:
        p = os.path.join(tmp, "beliefs.json")
        open(p, "w").write(text)
        bl = b.load(p, {"beliefs": []})   # what check_beliefs / sync_beliefs / add_belief do first
        b.save(p, bl)                       # ...and then write back
        print("%-20s -> beliefs on disk after one bw run: %d" % (label, len(json.load(open(p))["beliefs"])))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
