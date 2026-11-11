#!/bin/bash
# Cloud environment setup script for dogfooding bw in this project's threads.
# Paste into: Project settings > Cloud environment > Add cloud environment > Setup script.
# Also add the environment variable  BW_REPO=/mnt/project-files/bw-plugin  so bw watches the bw source
# even though threads start outside it. Every new thread then installs the latest bw from the shared folder.
# The shared folder is owned by another user than the session (root), so git refuses it as "dubious ownership"
# unless it is marked safe, under both the path threads use and the path it resolves to.
for d in /mnt/project-files/bw-plugin /mnt/attach/project-files/bw-plugin "$(readlink -f /mnt/project-files/bw-plugin 2>/dev/null)"; do
  [ -n "$d" ] && git config --global --add safe.directory "$d"
done
for i in $(seq 1 30); do [ -f /mnt/project-files/bw-plugin/install.sh ] && break; sleep 1; done
if [ -f /mnt/project-files/bw-plugin/install.sh ]; then
  bash /mnt/project-files/bw-plugin/install.sh || echo "bw: install failed (bw stays off, nothing else is affected)"
else
  echo "bw: /mnt/project-files is not mounted during setup; bw not installed"
fi
exit 0
