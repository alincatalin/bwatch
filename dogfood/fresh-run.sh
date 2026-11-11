#!/bin/bash
# See bw the way a new user does: a clean copy of a repo (its committed state, no .bw/ probes, no bw state), bw installed
# from this folder with install.sh into isolated Claude settings, then one real agent task steered only by bw's hooks.
#
#   bash dogfood/fresh-run.sh /path/to/repo "the ask, as a person would type it" [workdir]
#
# Isolation: CLAUDE_CONFIG_DIR is a fresh folder (no other plugins, no user-level hooks), BW_REPO is unset, and project
# memory and synced plugins/skills are turned off, so the agent knows only what the repo and bw tell it.
# Prints bw's own view afterwards: findings, receipt, stats, recall. Everything stays in the workdir.
set -euo pipefail
SRC="$(cd "$1" && pwd)"; ASK="$2"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
W="${3:-$(mktemp -d -t bw-fresh-XXXX)}"
mkdir -p "$W"
rm -rf "$W/repo" "$W/bw" "$W/cfg" "$W/src.git"
# Copy the git data (not the shared folder's ownership or its .git/bw state), then clone the committed state.
cp -r "$SRC/.git" "$W/src.git"; rm -rf "$W/src.git/bw"
git clone -q "$W/src.git" "$W/repo"
cd "$W/repo"
git config user.name "${BW_FRESH_USER:-new user}"; git config user.email "new-user@example.invalid"
if [ -d .bw ]; then git rm -rq .bw && git commit -qm "start: project without bw probes"; fi
mkdir -p "$W/bw"
cp -r "$HERE/plugin" "$HERE/install.sh" "$HERE/.claude-plugin" "$W/bw/"
export CLAUDE_CONFIG_DIR="$W/cfg"
unset BW_REPO CLAUDE_CODE_SYNC_PLUGINS CLAUDE_CODE_SYNC_SKILLS CLAUDE_MEMORY_STORES CLAUDE_COWORK_MEMORY_PATH_OVERRIDE || true
export BW_OPEN=0
echo "== install"
bash "$W/bw/install.sh" "$W/repo"
echo "== agent task: $ASK"
start=$(date +%s)
claude -p "$ASK" --permission-mode acceptEdits \
  --allowedTools "Read" "Grep" "Glob" "Edit" "Write" "Bash(python3:*)" "Bash(python:*)" "Bash(git:*)" "Bash(ls:*)" "Bash(cat:*)" "Bash(mkdir:*)" \
  --output-format stream-json --verbose > "$W/transcript.jsonl" 2> "$W/agent.err" || true
echo "   took $(( $(date +%s) - start )) s; transcript $W/transcript.jsonl"
python3 - "$W/transcript.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
mem = [r for r in rows if r.get("subtype") == "memory_recall"]
blocks = [r for r in rows if r.get("subtype") == "hook_response" and r.get("hook_event") == "Stop" and '"decision": "block"' in (r.get("stdout") or "")]
res = [r for r in rows if r.get("type") == "result"]
print("   memory notes recalled: %d; bw Stop-hook requests: %d" % (sum(len(r.get("memories", [])) for r in mem), len(blocks)))
for b in blocks:
    print("   bw asked: " + json.loads(b["stdout"])["reason"].splitlines()[0][:200])
if res:
    r = res[-1]
    print("   turns %s, cost $%.2f" % (r.get("num_turns"), r.get("total_cost_usd") or 0))
    print("\n== agent's last message\n" + (r.get("result") or "")[:3000])
EOF
BW="python3 $W/bw/plugin/scripts/bwcap.py"
echo; echo "== bw map";      $BW map
echo; echo "== bw findings"; $BW findings
echo; echo "== bw receipt (HEAD -> working tree)"; $BW receipt --from HEAD 2>/dev/null | head -80 || true
echo; echo "== bw stats";    $BW stats
echo; echo "== bw recall";   $BW recall
echo; echo "workdir: $W"
