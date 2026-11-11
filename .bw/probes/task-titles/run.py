import sys; sys.path.insert(0, "plugin/scripts")
import bwcap
cases = [
    ("plain prompt", "fix the export button on the orders page"),
    ("person's message in a wake", '<wake reason="mention"><thread><message from="human" author="Dana">add a retry to the importer</message></thread></wake>'),
    ("coordinator relay", '<coordinator-relay><relay from="coordinator"><cited author="user">review the billing changes</cited><note>start here</note></relay></coordinator-relay>'),
    ("status note only", '<wake reason="thread-computer-status"><project id="p"><thread ts="t"></thread></project>\n<system-note>status=linked</system-note>\n<system-note>Begin the work in the message above.</system-note></wake>'),
    ("pasted block only", "<pasted_content id=\"x\">log line 1\nlog line 2</pasted_content>"),
]
for name, raw in cases:
    p = bwcap.clean_prompt(raw)
    if not p:
        print("%-28s -> no task (no words from a person)" % name)
    else:
        print("%-28s -> %s" % (name, bwcap.task_title({"prompts": [p]})))
