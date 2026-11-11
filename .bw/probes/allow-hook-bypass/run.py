import importlib.util, os, sys
os.environ.pop("BW_REPO", None)
spec = importlib.util.spec_from_file_location("bwcap", "plugin/scripts/bwcap.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
me = os.path.abspath("plugin/scripts/bwcap.py")
cases = [
    ("real python3, plan wait", "python3 %s plan wait abcdef" % me),
    ("agent-written ./python3", "./python3 %s plan wait abcdef" % me),
    ("python3 from a temp dir", "/tmp/evil/python3 %s plan wait abcdef" % me),
    ("shell syntax appended", "python3 %s plan wait abcdef; id" % me),
    ("some other script", "python3 other.py plan wait abcdef"),
]
for label, cmd in cases:
    out = b.hook_allow({"tool_input": {"command": cmd}})
    print("%-26s -> %s" % (label, "AUTO-ALLOWED" if out else "asks the user"))
