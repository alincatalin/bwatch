# Plant-a-break check (`bwcap.py hollow`) on a tiny repo: a probe that drives both sides of each condition, and one
# that prints a single case. Breaks may only land on code, never on a docstring, a message or a comment.
import os, subprocess, sys, tempfile
BW = os.path.abspath("plugin/scripts/bwcap.py")
FEE = '''def late_fee(days):
    """No fee when days <= 3, then 5 a day, never more than 50 (days >= 0)."""
    msg = "days must be >= 0"  # not < 0
    if days < 0:
        raise ValueError(msg)
    if days <= 3:
        return 0
    return min(5 * days, 50)
'''
PROBE = 'import fee\nfor d in %s:\n    try: print(d, fee.late_fee(d))\n    except ValueError as e: print(d, e)\n'
with tempfile.TemporaryDirectory() as t:
    env = dict(os.environ, BW_HOME=os.path.join(t, "home"), BW_TELEMETRY="0", BW_NO_OPEN="1")
    r = os.path.join(t, "repo"); os.makedirs(r)
    open(os.path.join(r, "fee.py"), "w").write(FEE)
    for pid, days in (("table", "[-1, 0, 3, 4, 20]"), ("one-case", "[20]")):
        d = os.path.join(r, ".bw", "probes", pid); os.makedirs(d)
        open(os.path.join(d, "run.py"), "w").write(PROBE % days)
        open(os.path.join(d, "probe.json"), "w").write('{"scenario": "late fee for %s days", "cmd": "PYTHONPATH=. python3 -B .bw/probes/%s/run.py", "covers": ["fee.py"], "functions": ["late_fee"]}' % (days, pid))
    g = lambda *a: subprocess.run(("git", "-c", "user.name=t", "-c", "user.email=t@t") + a, cwd=r, capture_output=True)
    g("init", "-q"); g("add", "."); g("commit", "-qm", "fixture")
    for pid in ("table", "one-case"):
        o = subprocess.run([sys.executable, BW, "hollow", pid, "--n", "6"], cwd=r, env=env, capture_output=True, text=True)
        print("\n".join(l for l in o.stdout.splitlines() if not l.startswith("bw: turning")).strip() or o.stderr.strip()[-300:])
        print("[exit %d]" % o.returncode)
