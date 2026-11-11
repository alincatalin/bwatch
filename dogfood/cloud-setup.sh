#!/bin/bash
# Cloud environment setup script for dogfooding bw in this project's threads.
# Paste into: Project settings > Cloud environment > Add cloud environment > Setup script.
# Also add the environment variable  BW_REPO=/mnt/project-files/bw-plugin  so bw watches the bw source
# even though threads start outside it. Every new thread then installs the latest bw from the shared folder.
for i in $(seq 1 30); do [ -f /mnt/project-files/bw-plugin/install.sh ] && break; sleep 1; done
if [ -f /mnt/project-files/bw-plugin/install.sh ]; then
  bash /mnt/project-files/bw-plugin/install.sh || echo "bw: install failed (bw stays off, nothing else is affected)"
else
  echo "bw: /mnt/project-files is not mounted during setup; bw not installed"
fi
exit 0
