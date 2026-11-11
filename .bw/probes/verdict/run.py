import sys; sys.path.insert(0, "plugin/scripts")
import bwcap
o = lambda out, code: {"out": out, "exit": code}
cases = [("same output", o("a", 0), o("a", 0), True), ("different output", o("a", 0), o("b", 0), True),
         ("now crashes", o("a", 0), o("Traceback", 1), True), ("timed out after", o("a", 0), o("", 124), True),
         ("command not found", o("a", 0), o("", 127), True), ("not executable", o("", 126), o("", 126), True),
         ("differs between runs", o("a", 0), o("b", 0), False)]
for name, b, a, stable in cases:
    print("%-22s -> %s" % (name, bwcap.verdict(b, a, stable)))
