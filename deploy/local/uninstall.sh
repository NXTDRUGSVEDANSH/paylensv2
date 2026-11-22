#!/usr/bin/env bash
# Remove the service. Leaves the venv and the dataset alone.
set -euo pipefail
systemctl --user stop paylens.service 2>/dev/null || true
systemctl --user disable paylens.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/paylens.service"
systemctl --user daemon-reload
echo "paylens service removed (venv and data left in place)"
