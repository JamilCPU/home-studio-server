#!/usr/bin/env bash
# Deploy Home Studio to the Pi. Run from WSL (or any shell with rsync + ssh):
#   HOME_STUDIO_HOST=user@host bash scripts/deploy-pi3.sh
# The host is never committed. Add --setup to run setup-pi3.sh first.
set -euo pipefail

HOST="${HOME_STUDIO_HOST:?set HOME_STUDIO_HOST=user@host}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -o BatchMode=yes)

rsync -az -e "ssh -o BatchMode=yes" \
    --exclude 'data/*.db*' --exclude 'data/.legacy-migrated' \
    --exclude secrets --exclude config.local.yaml \
    --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.egg-info' \
    --exclude .superpowers --exclude .pytest_cache \
    "$REPO"/ "$HOST":home-studio/

if [ "${1:-}" = "--setup" ] || ! "${SSH[@]}" "$HOST" 'test -x ~/home-studio/.venv/bin/python'; then
    "${SSH[@]}" "$HOST" 'bash ~/home-studio/scripts/setup-pi3.sh'
fi

"${SSH[@]}" "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/home-studio
if [ ! -f config.local.yaml ]; then
    cat > config.local.yaml <<'CFG'
youtube:
  api_key: ""
  cookies_path: "secrets/cookies.txt"
bluetooth:
  speaker_mac: ""
CFG
    chmod 600 config.local.yaml
fi
mkdir -p ~/.config/systemd/user
cp systemd/home-studio.service systemd/ytdlp-update.service systemd/ytdlp-update.timer    ~/.config/systemd/user/
chmod +x scripts/*.sh
systemctl --user daemon-reload
.venv/bin/pip install -q -e . </dev/null
if [ ! -f data/.legacy-migrated ]; then
    .venv/bin/python -m src --config config.yaml migrate-legacy --data-dir data </dev/null
    touch data/.legacy-migrated
fi
systemctl --user restart home-studio
for _ in $(seq 1 10); do
    if curl -fsS localhost:8765/api/status >/dev/null 2>&1; then
        echo "health check ok"
        exit 0
    fi
    sleep 1
done
echo "ERROR: service did not become healthy" >&2
systemctl --user status home-studio --no-pager >&2 || true
journalctl --user -u home-studio -n 30 --no-pager >&2 || true
exit 1
REMOTE
echo "Deployed. Check: ssh \$HOME_STUDIO_HOST 'curl -s localhost:8765/api/status'"
