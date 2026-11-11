import sys; sys.path.insert(0, "plugin/scripts")
import bwcap
for p in ["code review the changes", "code review tge uncommited changes", "fix the most important ones", "do it",
          "how does the paywall decide a trial ended?", "commit all the changes", "investigate why sync drops steps",
          "implement all the fixes", "explain the merge flow"]:
    print("%-45s -> %s" % (p, "review" if bwcap.REVIEW_PROMPT.search(p) else "-"))
