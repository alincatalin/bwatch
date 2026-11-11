import sys; sys.path.insert(0, "plugin/scripts")
import bwcap
text = "Findings:\n1. NOT SHOWN: purchase restore needs a device - store sandbox only\nNOT SHOWN:  push opens wrong screen - needs a phone\nnot shown: lower case is ignored\n"
for l in bwcap.finding_lines(text):
    print("not shown:", l)
print("count:", len(bwcap.finding_lines(text)))
