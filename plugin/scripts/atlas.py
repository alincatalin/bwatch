#!/usr/bin/env python3
"""bw atlas: build a model of what a project does from what bw has actually observed, and render it as one page.

Every fact in the model carries the kind of evidence behind it, and the page never mixes them up:
  ran      a probe ran this situation through the real code and printed this (bw ran it twice; same output)
  broke    bw planted small breaks in the covered code and the probe's output caught k of n
  read     a line of code bw read at this snapshot (the file and line exist and the text is quoted from it)
  doc      a line a project doc states (bw found the quote in the doc)
  says     the agent's words; nothing checked them
  gap      something nobody could show, with the reason
Derived, never invented:
  cases     probe output lines "situation -> key=value ..." parsed into a table
  history   the probe's output at every code snapshot bw ran it on (from bw's observation cache), cell by cell
  rules     named constants in the code a probe covers (UPPER_CASE = value), read in code
  readers   lines elsewhere in the code that name a covered file's main type or a probe's functions or rules
  links     two behaviors whose names appear within a few lines of each other in the same file
  checks    agent assertions ("case X prints field=value") compared with what the probe printed

stdlib only. Usage: python3 atlas.py [--root DIR] [--overlay FILE] [--out FILE] [--json FILE]
The overlay is the agent's structured output (.bw/understanding.json by default): capability names, assertions,
questions with cited steps. bw checks every citation in it; anything it cannot check stays marked as the agent's.
"""
import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".kt", ".kts", ".java", ".swift", ".go", ".rs", ".rb", ".php",
            ".cs", ".scala", ".ex", ".exs", ".dart", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".vue", ".svelte", ".sql"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|androidTest|androidUnitTest|commonTest|jvmTest|iosTest|unitTest)(/|$)|"
                       r"(Test|Tests|_test|\.test|\.spec|_spec)\.\w+$")
SKIP_PATH = re.compile(r"(^|/)(\.bw|node_modules|build|dist|vendor|third_party|\.gradle|Pods)(/|$)")
CONST_RX = re.compile(r"^\s*(?:(?:export|public|private|internal|protected|static|final|readonly|pub)\s+)*"
                      r"(?:const\s+val|val|const|let|var|final|static\s+final\s+\w+|pub\s+const|#define)?\s*"
                      r"([A-Z][A-Z0-9_]{2,})\s*(?::\s*[\w<>?, ]+)?\s*=\s*(.{1,120}?)\s*;?\s*$")
CASE_RX = re.compile(r"^(.*?)\s*(?:->|=>|→)\s*(.*)$")
KV_RX = re.compile(r"([A-Za-z_][\w.]*)=(\[[^\]]*\]|\"[^\"]*\"|\S+)")
LINK_WINDOW = 6


def git(args, root, check=False):
    r = subprocess.run(["git"] + args, cwd=root, capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


# ---------- cases: probe output as a table ----------

def parse_cases(out):
    """'free: a=1 b=2' or 'day 9, 7 celebrated -> null' lines become {label, fields}. Lines that fit neither are kept raw."""
    cases, order = [], []
    for ln in (out or "").splitlines():
        s = ln.strip()
        if not s or re.match(r"^\[exit -?\d+\]$", s):
            continue
        m = CASE_RX.match(s)
        label, rest = (m.group(1), m.group(2)) if m else (None, None)
        if label is None:
            m2 = re.match(r"^([^=:]{1,80}):\s+(.*\S.*)$", s)
            if m2:
                label, rest = m2.group(1), m2.group(2)
        if label is None:
            cases.append({"label": s, "fields": {}, "raw": s, "unparsed": 1})
            continue
        kvs = KV_RX.findall(rest)
        if kvs and len(" ".join("%s=%s" % kv for kv in kvs)) >= len(rest.strip()) * 0.6:
            fields = {k: v for k, v in kvs}
        else:
            fields = {"→": rest.strip()}
        for k in fields:
            if k not in order:
                order.append(k)
        cases.append({"label": label.strip(), "fields": fields, "raw": s})
    return cases, order


def exit_of(out):
    m = re.search(r"\[exit (-?\d+)\]\s*$", out or "")
    return int(m.group(1)) if m else None


def cell_diff(a_cases, b_cases):
    """Cells that differ between two case tables, matched by label. Labels only on one side are added/removed rows."""
    A = {c["label"]: c for c in a_cases}
    B = {c["label"]: c for c in b_cases}
    cells = []
    for lab, cb in B.items():
        ca = A.get(lab)
        if not ca:
            continue
        for k in sorted(set(ca["fields"]) | set(cb["fields"])):
            va, vb = ca["fields"].get(k), cb["fields"].get(k)
            if va != vb:
                cells.append({"label": lab, "field": k, "before": va, "after": vb})
    return {"cells": cells, "added": [l for l in B if l not in A], "removed": [l for l in A if l not in B]}


# ---------- snapshots and history ----------

def snapshots(root):
    """Every code snapshot bw made (refs/bw/<task>/<label>), keyed by the first 16 chars of its tree, earliest first."""
    out = {}
    for ln in git(["for-each-ref", "refs/bw", "--format=%(refname) %(objectname) %(committerdate:unix)"], root).splitlines():
        parts = ln.split()
        if len(parts) != 3:
            continue
        ref, commit, t = parts
        tree = git(["rev-parse", commit + "^{tree}"], root).strip()[:16]
        bits = ref.split("/")
        task = bits[2] if len(bits) > 3 else ""
        label = bits[-1]
        if tree and (tree not in out or int(t) < out[tree]["at"]):
            out[tree] = {"tree": tree, "commit": commit[:10], "at": int(t), "task": task, "label": label}
    return out


def task_title(bwd, tid):
    t = load(os.path.join(bwd, "tasks", tid, "task.json"), {}) or {}
    p = (t.get("prompts") or [""])[0]
    p = re.sub(r"\s+", " ", p).strip()
    if p.startswith("<"):  # machine-wrapped prompts are not a title
        return ""
    # a prompt is the person's own words and may hold a key or token: never carry those into a page that can be shared
    p = re.sub(r"\b[A-Za-z0-9_\-]*(?:[A-Za-z][0-9]|[0-9][A-Za-z]|_)[A-Za-z0-9_\-]{14,}\b", "[redacted]", p)
    p = re.sub(r"(?i)\b(token|key|secret|password|passwd|bearer)\b\s*[:=]?\s*\S+", r"\1 [redacted]", p)
    return p[:80]


# ---------- code reading ----------

def code_files(root):
    fs = [f for f in git(["ls-files", "-co", "--exclude-standard"], root).splitlines() if f]
    return [f for f in fs if os.path.splitext(f)[1].lower() in CODE_EXT and not SKIP_PATH.search(f)]


def read_lines(root, path, cache={}):
    if path not in cache:
        try:
            with open(os.path.join(root, path), errors="replace") as f:
                cache[path] = f.read().split("\n")
        except Exception:
            cache[path] = None
    return cache[path]


def at_line(root, path, line):
    ls = read_lines(root, path)
    if ls is None or not line or line > len(ls):
        return None
    return ls[line - 1]


def check_quote(root, path, line, quote):
    """True when the quote is on that line (or within 2 lines: small edits move lines). Returns (ok, found_line, text)."""
    ls = read_lines(root, path)
    if ls is None:
        return False, None, None
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    q = norm(quote)
    for d in (0, -1, 1, -2, 2):
        i = (line or 1) - 1 + d
        if 0 <= i < len(ls) and q and q in norm(ls[i]):
            return True, i + 1, ls[i].strip()
    return False, None, None


def is_copy(text, sym):
    """The symbol sits inside a string literal on that line: it is shown to people, not used to decide."""
    for m in re.finditer(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`', text):
        if sym in m.group(0):
            return True
    return False


def is_comment(text):
    """The whole line is a comment or docstring text: it names the symbol but decides nothing."""
    return bool(re.match(r'^\s*(//|/\*|\*|#(?!include|if|define|pragma)|--|<!--|"""|\'\'\')', text))


def probe_key_of(root, pid, spec):
    """The key bw files this probe's observations under: sha1 of its dir, command and the files beside probe.json."""
    files = {}
    d = os.path.join(root, ".bw", "probes", pid)
    for dp, _, fns in os.walk(d):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, d).replace(os.sep, "/")
            if rel == "probe.json":
                continue
            try:
                txt = open(full, encoding="utf-8").read()
            except Exception:
                continue
            if len(txt) <= 64 * 1024:
                files[rel] = txt
    return hashlib.sha1(json.dumps({"dir": pid, "cmd": spec.get("cmd"), "files": files}, sort_keys=True).encode()).hexdigest()[:16]


def symbols_for(p, root):
    """Names other code would use to reach this behavior: the main type of each covered file (its file name),
    the probe's functions (Type.member), and the UPPER_CASE rules declared in the covered files."""
    syms = []
    for c in p.get("covers", []):
        stem = os.path.splitext(os.path.basename(c))[0]
        if re.match(r"^[A-Za-z_]\w{3,}$", stem):
            syms.append({"name": stem, "rx": r"\b%s\b" % re.escape(stem), "kind": "type"})
    for fn in p.get("functions", []) or []:
        if "." in fn:
            cls, mem = fn.rsplit(".", 1)
            first = cls[0]
            rx = r"\b[%s%s]%s\.%s\b" % (first.upper(), first.lower(), re.escape(cls[1:]), re.escape(mem))
            syms.append({"name": fn, "rx": rx, "kind": "function"})
    return syms


def rules_in(root, covers):
    out = []
    for c in covers:
        ls = read_lines(root, c) or []
        for i, ln in enumerate(ls):
            m = CONST_RX.match(ln)
            if m and not ln.strip().startswith(("//", "#", "*")) and not re.search(r",\s*\w+\s*=", ln) and not ln.rstrip().endswith((",", "(")):
                out.append({"name": m.group(1), "value": m.group(2).strip().rstrip(","), "file": c, "line": i + 1,
                            "text": ln.strip()[:200]})
    return out


def grep_lines(root, files, rx):
    """(file, line, text) for every line matching rx in the given files. Skips import lines."""
    hits = []
    r = re.compile(rx)
    for f in files:
        ls = read_lines(root, f)
        if not ls:
            continue
        for i, ln in enumerate(ls):
            if r.search(ln) and not re.match(r"^\s*(import|from|using|#include|package)\b", ln):
                hits.append((f, i + 1, ln.strip()[:220]))
    return hits


# ---------- the model ----------

def build(root, overlay_path=None):
    root = os.path.abspath(root)
    common = git(["rev-parse", "--git-common-dir"], root).strip()
    bwd = os.path.join(root, common, "bw") if not os.path.isabs(common) else os.path.join(common, "bw")
    res = load(os.path.join(bwd, "onboard", "result.json"), {}) or {}
    scan = res.get("scan") or load(os.path.join(bwd, "onboard", "scan.json"), {}) or {}
    obs = load(os.path.join(bwd, "obs.json"), {}) or {}
    findings = (load(os.path.join(bwd, "findings.json"), {}) or {}).get("findings", {})
    decisions = (load(os.path.join(bwd, "decisions.json"), {}) or {}).get("decisions", [])
    beliefs = (load(os.path.join(bwd, "beliefs.json"), {}) or {}).get("beliefs", [])
    ov_path = overlay_path or os.path.join(root, ".bw", "understanding.json")
    ov = load(ov_path, {}) or {}
    snaps = snapshots(root)
    head_tree = git(["rev-parse", "HEAD^{tree}"], root).strip()[:16]
    head_at = int(git(["log", "-1", "--format=%ct"], root).strip() or 0)

    def head_snap(tree):
        if tree == head_tree:
            return {"tree": tree, "at": head_at, "task": "", "label": "last commit"}
        return {}
    files = code_files(root)
    nontest = [f for f in files if not TEST_PATH.search(f)]
    tests = [f for f in files if TEST_PATH.search(f)]

    # probes, as written now
    probes = {}
    pdir = os.path.join(root, ".bw", "probes")
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        spec = load(os.path.join(pdir, pid, "probe.json"))
        if isinstance(spec, dict) and spec.get("cmd"):
            spec["id"] = pid
            probes[pid] = spec
    rows = {r["probe"]: r for r in res.get("rows", [])}

    # Observations by probe. obs.json is keyed "<tree16>:<probe key>"; the probe key changes when the probe is edited,
    # so a probe's history is every key whose outputs share its case labels with the current output.
    by_tree = {}
    for k, v in obs.items():
        tree, pk = k.split(":", 1)
        by_tree.setdefault(pk, []).append((tree, v))

    behaviors = []
    for pid, p in probes.items():
        row = rows.get(pid, {})
        now_out = row.get("after", "")
        src = "onboard" if row else None
        if not row:  # never onboarded: use the newest run bw kept for this exact probe
            mine = by_tree.get(probe_key_of(root, pid, p), [])
            timed = sorted(mine, key=lambda tv: ((snaps.get(tv[0]) or head_snap(tv[0])).get("at") or 0))
            if timed:
                v = timed[-1][1]
                now_out = v.get("out", "")
                row = {"stable": v.get("stable"), "ms": v.get("ms")}
                src = "task"
        cases, fields = parse_cases(now_out)
        b = {"id": pid, "claim": p.get("finding") or p.get("scenario", ""), "scenario": p.get("scenario", ""),
             "area": p.get("area") or row.get("area", ""), "kind": p.get("kind", "behavior"), "cmd": p.get("cmd", ""),
             "covers": p.get("covers", []), "functions": p.get("functions", []), "out": now_out,
             "cases": cases, "fields": fields, "exit": exit_of(now_out),
             "source": src, "ran": {"runs": 2 if src == "onboard" else 1 if src else 0, "stable": row.get("stable"), "ms": row.get("ms"), "problems": row.get("problems", [])},
             "broke": ({"caught": row["hollow"][0], "tried": row["hollow"][1], "missed": row.get("missed") or []}
                       if row.get("hollow") else None)}
        # entry: where a user's way in starts
        if p.get("entry"):
            m = re.match(r"^(.+?):(\d+)", p["entry"])
            if m:
                txt = at_line(root, m.group(1), int(m.group(2)))
                b["entry"] = {"file": m.group(1), "line": int(m.group(2)), "text": (txt or "").strip()[:200], "ok": txt is not None}
            else:
                b["entry"] = {"text": p["entry"], "ok": None}
        # a doc rule the probe was written for
        if p.get("kind") == "rule" and p.get("quote"):
            src = read_lines(root, p.get("source", "")) or []
            found = [i + 1 for i, ln in enumerate(src) if p["quote"].strip() in ln]
            b["doc"] = {"source": p.get("source"), "quote": p["quote"], "line": found[0] if found else None,
                        "found": bool(found), "is_code": bool(re.search(r"\b(const|val|def|function|class)\b|[=;{}]", p["quote"]))}
        # history: outputs at every snapshot, oldest first, as case tables
        hist = []
        labels_now = {c["label"] for c in cases if not c.get("unparsed")}
        fields_now = set(fields)
        for pk, lst in by_tree.items():
            outs = [v.get("out", "") for _, v in lst]
            if now_out not in outs:  # an edited probe has a new key: accept it only if it prints the same rows and fields
                ok_any = False
                for o in outs:
                    cs, fs = parse_cases(o)
                    labs = {c["label"] for c in cs if not c.get("unparsed")}
                    if labels_now and set(fs) == fields_now and len(labs & labels_now) >= max(2, len(labels_now) // 2):
                        ok_any = True
                if not ok_any:
                    continue
            for tree, v in lst:
                cs, _ = parse_cases(v.get("out", "")) if v.get("exit") in (0, None) else ([], [])
                s = snaps.get(tree) or head_snap(tree)
                hist.append({"tree": tree, "at": s.get("at"), "task": s.get("task", ""), "label": s.get("label", ""),
                             "title": task_title(bwd, s["task"]) if s.get("task") else "", "out": v.get("out", ""),
                             "exit": v.get("exit"), "cases": cs, "pk": pk})
        hist.sort(key=lambda h: (h["at"] is None, h["at"] or 0))
        seen, dedup = set(), []
        for h in hist:  # one entry per tree
            if h["tree"] in seen:
                continue
            seen.add(h["tree"])
            dedup.append(h)
        changes = []
        timed = [h for h in dedup if h["at"] and h.get("exit") in (0, None)]  # a run that failed says nothing about behavior
        for a, z in zip(timed, timed[1:]):
            if a["out"] != z["out"] and a.get("exit") == 0 and z.get("exit") == 0:
                d = cell_diff(a["cases"], z["cases"])
                if d["cells"] or d["added"] or d["removed"]:
                    changes.append(dict(d, frm=a["at"], to=z["at"], task=z["task"], title=z["title"], frm_tree=a["tree"], to_tree=z["tree"]))
        b["history"] = [dict({k: h[k] for k in ("tree", "at", "task", "label", "title", "exit")},
                             why=fail_why(h["out"]) if h.get("exit") not in (0, None) else None) for h in dedup]
        b["changes"] = changes
        # the claim, as first recorded: did the output it was written against change since?
        recs = sorted([f for f in findings.values() if f.get("pid") == pid and f.get("out")], key=lambda f: f.get("at", 0))
        if recs:
            first = recs[0]
            if first["out"].strip() != now_out.strip():
                d = cell_diff(parse_cases(first["out"])[0], cases)
                b["claim_stale"] = {"at": first.get("at"), "cells": d["cells"], "task": first.get("task")}
        b["decisions"] = [{"decision": d.get("decision"), "at": d.get("at"), "pin": (d.get("pin") or "")[:10], "want": d.get("want", "")[:300]}
                          for d in decisions if (d.get("probe") or "").rstrip("/").endswith("/" + pid) or d.get("id", "").endswith("-f-" + pid)]
        b["belief"] = next((x for x in beliefs if x.get("probe") == pid or x.get("id") == pid), None)
        b["rules"] = rules_in(root, b["covers"])
        if len(b["rules"]) > 12:  # a big shared file: keep only the constants this probe's own files or wording name
            ptxt = " ".join([p.get("finding", ""), p.get("scenario", ""), p.get("cmd", "")])
            pd = os.path.join(root, ".bw", "probes", pid)
            for dp, _, fns in os.walk(pd):
                for fn in fns:
                    try:
                        ptxt += "\n" + open(os.path.join(dp, fn), encoding="utf-8").read()
                    except Exception:
                        pass
            keep = [r for r in b["rules"] if re.search(r"\b%s\b" % re.escape(r["name"]), ptxt)]
            b["rules_more"] = len(b["rules"]) - len(keep)
            b["rules"] = keep
        # readers: who names this behavior's types, functions and rules, outside the covered files
        syms = symbols_for(p, root) + [{"name": r["name"], "rx": r"\b%s\b" % re.escape(r["name"]), "kind": "rule"} for r in b["rules"]]
        readers = []
        others = [f for f in nontest if f not in b["covers"]]
        for s in syms:
            for f, ln, txt in grep_lines(root, others, s["rx"]):
                readers.append({"file": f, "line": ln, "text": txt, "sym": s["name"], "symkind": s["kind"],
                                "use": "comment" if is_comment(txt) else "copy" if is_copy(txt, s["name"].split(".")[-1]) else "logic"})
        test_hits = []
        for s in syms:
            test_hits += [{"file": f, "line": ln, "text": t, "sym": s["name"]} for f, ln, t in grep_lines(root, tests, s["rx"])]
        b["readers"] = readers
        b["test_refs"] = test_hits
        b["_syms"] = syms
        behaviors.append(b)

    # links: two behaviors named within LINK_WINDOW lines of each other in one file (outside both behaviors' own files)
    cand = []
    idx = {}
    for b in behaviors:
        for r in b["readers"]:
            idx.setdefault(r["file"], []).append((r["line"], b["id"], r))
    for f, hits in idx.items():
        hits.sort(key=lambda h: (h[0], h[1]))
        for i, (la, ba, ra) in enumerate(hits):
            for lb, bb, rb in hits[i + 1:]:
                if lb - la > LINK_WINDOW:
                    break
                if ba == bb or ra["sym"] == rb["sym"]:  # the same name read twice is not a hand-off between two behaviors
                    continue
                feeds = None
                if la == lb:  # same line: one name inside the other's call parentheses means its value is handed over
                    t = ra["text"]
                    na, nb = ra["sym"].split(".")[-1], rb["sym"].split(".")[-1]
                    if re.search(re.escape(na) + r"\w*(?:\.\w+)?\([^)]*" + re.escape(nb), t):
                        feeds = bb + ">" + ba
                    elif re.search(re.escape(nb) + r"\w*(?:\.\w+)?\([^)]*" + re.escape(na), t):
                        feeds = ba + ">" + bb
                cand.append({"a": ba, "b": bb, "file": f, "la": la, "lb": lb, "ta": ra["text"], "tb": rb["text"],
                             "sa": ra["sym"], "sb": rb["sym"], "feeds": feeds, "dist": lb - la})
    # per pair of behaviors keep the closest few places, a hand-off first, one per line
    links, per = [], {}
    sharing = {tuple(sorted((a["id"], z["id"]))) for a in behaviors for z in behaviors if a["id"] != z["id"] and set(a["covers"]) & set(z["covers"])}
    cand = [c for c in cand if tuple(sorted((c["a"], c["b"]))) not in sharing]  # built on the same file: shown as "same code", not a hand-off
    for c in sorted(cand, key=lambda c: (c["feeds"] is None, c["dist"], c["file"], c["la"])):
        k = tuple(sorted((c["a"], c["b"])))
        lst = per.setdefault(k, [])
        if len(lst) >= 4 or any(x["file"] == c["file"] and abs(x["la"] - c["la"]) <= LINK_WINDOW for x in lst):
            continue
        lst.append(c)
        links.append(c)
    shares = []
    for i, a in enumerate(behaviors):
        for z in behaviors[i + 1:]:
            common_files = sorted(set(a["covers"]) & set(z["covers"]))
            if common_files:
                shares.append({"a": a["id"], "b": z["id"], "files": common_files})

    # the agent's structured output, checked
    ids = {b["id"]: b for b in behaviors}
    for pid, asserts in (ov.get("assertions") or {}).items():
        b = ids.get(pid)
        if not b:
            continue
        out = []
        for a in asserts:
            c = next((c for c in b["cases"] if c["label"] == a.get("case")), None) if a.get("case") else None
            got = c["fields"].get(a.get("field") or "→") if c else None
            st = "unchecked" if not a.get("case") else ("no-case" if not c else ("match" if str(got) == str(a.get("value")) else "differs"))
            out.append({"says": a.get("says", ""), "case": a.get("case"), "field": a.get("field") or "→",
                        "expect": a.get("value"), "got": got, "status": st})
        b["assertions"] = out
    for pid, st in (ov.get("states") or {}).items():
        b = ids.get(pid)
        if not b:
            continue
        rule = next((r for r in b["rules"] if r["name"] == st.get("rule")), None)
        st = dict(st)
        if rule:
            st["values"] = [int(x) for x in re.findall(r"-?\d+", rule["value"])]
            st["rule_at"] = {"file": rule["file"], "line": rule["line"], "text": rule["text"]}
        for t in st.get("notes", []):
            if t.get("file"):
                ok, ln, txt = check_quote(root, t["file"], t.get("line"), t.get("quote", ""))
                t.update(ok=ok, line=ln or t.get("line"), text=txt)
        b["state"] = st
    questions = []
    for q in ov.get("questions", []):
        q = json.loads(json.dumps(q))
        for s in q.get("steps", []):
            if s.get("kind") == "read" and s.get("file"):
                ok, ln, txt = check_quote(root, s["file"], s.get("line"), s.get("quote", ""))
                s.update(ok=ok, line=ln or s.get("line"), text=txt)
                if not ok:
                    s["kind"] = "says"  # a citation bw could not find is only the agent's word
                    s["why"] = "the quoted text is not at %s:%s" % (s["file"], s.get("line"))
            if s.get("kind") == "ran" and s.get("behavior"):
                b = ids.get(s["behavior"])
                c = next((c for c in (b or {}).get("cases", []) if c["label"] == s.get("case")), None)
                s["ok"] = bool(c)
                s["got"] = c["fields"] if c else None
                if not c:
                    s["kind"] = "says"
                    s["why"] = "probe %s has no case %r" % (s["behavior"], s.get("case"))
        questions.append(q)
    for b in ids.values():
        b.pop("_syms", None)

    # areas: implementation size and activity, kept apart from behavior
    summ = {a.get("area"): a for a in (res.get("project") or []) if isinstance(a, dict)}
    areas = []
    for a in scan.get("areas", []):
        if not a.get("files"):
            continue
        sm = summ.get(a["name"], {})
        areas.append({"name": a["name"], "paths": a.get("paths", []), "files": a["files"], "commits": a.get("commits"),
                      "fixes": a.get("fixes"), "tests": a.get("tests"), "test_count": a.get("test_count", 0),
                      "test_names": a.get("test_names", [])[:6], "focus": a["name"] in scan.get("focus", []),
                      "summary": sm.get("summary", ""), "entry": sm.get("entry", ""), "entry_ok": sm.get("entry_ok"),
                      "behaviors": [b["id"] for b in behaviors if b["area"] == a["name"]]})
    caps = ov.get("capabilities") or []
    placed = {x for c in caps for x in c.get("behaviors", [])}
    rest = [b["id"] for b in behaviors if b["id"] not in placed]
    if rest:
        caps = caps + [{"name": "Not grouped yet", "behaviors": rest, "computed": 1}]
    unshown = []
    for t in sorted(os.listdir(os.path.join(bwd, "tasks"))) if os.path.isdir(os.path.join(bwd, "tasks")) else []:
        d = load(os.path.join(bwd, "tasks", t, "data.json"), {}) or {}
        for u in d.get("unshown") or []:
            if u not in unshown:
                unshown.append(u)
    snaps_list = sorted(snaps.values(), key=lambda s: s["at"])
    for s in snaps_list:
        s["title"] = task_title(bwd, s["task"]) if s.get("task") else ""
    model = {"repo": os.path.basename(root), "head": git(["rev-parse", "--short", "HEAD"], root).strip(),
             "dirty": len([l for l in git(["status", "--short"], root).splitlines() if l.strip()]),
             "built": int(time.time()), "onboarded": int(res.get("at") or 0), "commit": (res.get("commit") or "")[:10],
             "scan": {k: scan.get(k) for k in ("files", "code_files", "languages", "commits_12m", "tests", "test_names", "test_commands", "docs")},
             "areas": areas, "behaviors": behaviors, "links": links, "shares": shares, "capabilities": caps,
             "questions": questions, "boundaries": ov.get("boundaries", []), "unshown": unshown, "snapshots": snaps_list,
             "equivalents": [e for e in ov.get("equivalents", []) if isinstance(e, dict) and e.get("a") and e.get("b")],
             "decisions": len(decisions), "overlay": os.path.basename(ov_path) if ov else None}
    return model


def fail_why(out):
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    for pat in ("error:", "Unresolved", "Error", "FAILED", "Exception"):
        for l in lines:
            if pat in l and not l.startswith("BUILD FAILED"):
                return l[:200]
    return (lines[-1] if lines else "")[:200]


def render(model, out_path):
    """model: one model, or a list of models (one per repository) shown behind a repository switcher."""
    models = model if isinstance(model, list) else [model]
    tpl = open(os.path.join(HERE, "atlas_template.html")).read()
    data = json.dumps(models, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("/*__MODEL__*/null", data)
    if not page.lstrip().lower().startswith("<!doctype"):
        page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n' + page
    with open(out_path, "w") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--overlay")
    ap.add_argument("--out")
    ap.add_argument("--json")
    ap.add_argument("--with", dest="more", nargs="*", default=[], help="model JSON files of other repositories to show in the same page")
    a = ap.parse_args()
    m = build(a.root, a.overlay)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(m, f, indent=1)
    if a.out:
        render([m] + [json.load(open(f)) for f in a.more], a.out)
        print("atlas: %d behaviors, %d links, %d snapshots -> %s" % (len(m["behaviors"]), len(m["links"]), len(m["snapshots"]), a.out))
    if not a.json and not a.out:
        json.dump(m, sys.stdout)


if __name__ == "__main__":
    main()
