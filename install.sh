#!/bin/bash
# bw: one-command install.
#   bash install.sh                      install the Claude Code plugin (does nothing in any repo yet)
#   bash install.sh REPO [MAP.json]      install, then turn bw on in REPO (map drafted from folders unless MAP given)
# Needs: claude (Claude Code), python3, git. Keep this folder: Claude Code installs the plugin from it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
for t in claude python3 git; do command -v "$t" >/dev/null || { echo "bw: '$t' is not installed or not on PATH"; exit 1; }; done
if claude plugin marketplace list 2>/dev/null | grep -q "bw-local"; then
  claude plugin marketplace update bw-local >/dev/null
else
  claude plugin marketplace add "$HERE" >/dev/null
fi
# Reinstall every time: Claude Code runs hooks from its own cached copy, and an update can leave the old copy in use.
if claude plugin list 2>/dev/null | grep -q "bw@bw-local"; then
  claude plugin uninstall bw@bw-local >/dev/null 2>&1 || true
fi
claude plugin install bw@bw-local >/dev/null
VER=$(claude plugin list 2>/dev/null | grep -A1 "bw@bw-local" | grep -o "Version: [0-9.]*" || true)
echo "bw plugin installed ($VER). It stays silent until you turn it on in a repo."
echo "Quit every open Claude Code session and start a new one, or the old version keeps running."
if [ $# -ge 1 ]; then
  if [ $# -ge 2 ]; then python3 "$HERE/plugin/scripts/bwcap.py" on "$1" --map "$2"; else python3 "$HERE/plugin/scripts/bwcap.py" on "$1"; fi
else
  echo "Turn it on in a repo:  python3 \"$HERE/plugin/scripts/bwcap.py\" on /path/to/repo"
fi
