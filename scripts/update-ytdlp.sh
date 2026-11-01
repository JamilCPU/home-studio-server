#!/usr/bin/env bash
# Upgrade yt-dlp in the venv; restart the service only if the version changed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY=.venv/bin/python
ver() { "$PY" -c "import yt_dlp.version as v; print(v.__version__)"; }
before="$(ver)"
.venv/bin/pip install -q -U yt-dlp
after="$(ver)"
if [ "$before" != "$after" ]; then
    echo "yt-dlp $before -> $after, restarting home-studio"
    systemctl --user restart home-studio.service
else
    echo "yt-dlp unchanged ($after)"
fi
