import sys; sys.path.insert(0, "plugin/scripts")
import bwcap
text = "Findings:\n1. NOT SHOWN: card retry after a bank timeout - needs the live payment network\nNOT SHOWN:  export email has a broken link - needs a mail server\nnot shown: lower case is ignored\n"
for l in bwcap.finding_lines(text):
    print("not shown:", l)
print("count:", len(bwcap.finding_lines(text)))
