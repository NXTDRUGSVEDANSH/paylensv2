#!/usr/bin/env bash
# One-command local deployment.
#
#   ./deploy/local/install.sh
#
# Idempotent: safe to re-run after a code change (it reinstalls the unit and
# restarts). Builds the venv, installs deps, generates + VERIFIES the dataset,
# then installs a systemd user service that restarts on failure.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${PAYLENS_VENV:-$HOME/.venvs/paylens}"
PORT="${PORT:-8000}"
UNIT_DIR="$HOME/.config/systemd/user"

say() { printf '\033[0;32m▸\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. interpreter ───────────────────────────────────────────────────────────
# 3.12 specifically: chromadb needs onnxruntime, which has no 3.14 wheels yet.
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv at $VENV (python 3.12)"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.12
  else
    command -v python3.12 >/dev/null 2>&1 || die "need python3.12 or uv on PATH"
    python3.12 -m venv "$VENV"
  fi
else
  say "venv exists: $VENV"
fi
PY="$VENV/bin/python"
"$PY" --version | grep -q "3\.12" || echo "  warning: expected python 3.12, got $("$PY" --version)"

# ── 2. dependencies ──────────────────────────────────────────────────────────
say "installing dependencies"
if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="$VENV" uv pip install -q -r "$PROJECT_DIR/requirements.txt"
else
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r "$PROJECT_DIR/requirements.txt"
fi

# ── 3. data ──────────────────────────────────────────────────────────────────
if [ -f "$PROJECT_DIR/data/paylens.duckdb" ] && [ "${REGENERATE:-0}" != "1" ]; then
  say "dataset present (REGENERATE=1 to rebuild)"
else
  say "generating dataset (deterministic, seed=42)"
  (cd "$PROJECT_DIR/data" && "$PY" generate.py)
fi
say "verifying calibration and planted defects"
(cd "$PROJECT_DIR" && "$PY" data/verify.py) || die "dataset verification FAILED — refusing to deploy a dataset whose calibration has drifted"

# ── 4. service ───────────────────────────────────────────────────────────────
say "installing systemd user service"
mkdir -p "$UNIT_DIR"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__VENV__|$VENV|g" \
    "$PROJECT_DIR/deploy/local/paylens.service" > "$UNIT_DIR/paylens.service"
systemctl --user daemon-reload
systemctl --user enable paylens.service >/dev/null 2>&1 || true
systemctl --user restart paylens.service

# Survive logout. Needs root, so it is attempted and not required.
loginctl enable-linger "$USER" >/dev/null 2>&1 \
  && say "lingering enabled — service survives logout" \
  || echo "  note: could not enable lingering (needs sudo); the service stops when you log out"

# ── 5. wait for health ───────────────────────────────────────────────────────
say "waiting for health (the ONNX model load takes ~25s on a cold start)"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo
    curl -fsS "http://127.0.0.1:$PORT/health" | "$PY" -m json.tool
    WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    say "PayLens is up"
    echo "    http://localhost:$PORT            (from Windows too, via WSL localhost forwarding)"
    [ -n "${WSL_IP:-}" ] && echo "    http://$WSL_IP:$PORT   (direct WSL address, if the above fails)"
    echo
    echo "    status   systemctl --user status paylens"
    echo "    logs     journalctl --user -u paylens -f"
    echo "    stop     systemctl --user stop paylens"
    exit 0
  fi
  sleep 2
done
echo
systemctl --user status paylens --no-pager -l | head -25
die "did not become healthy in 120s — see the status above"
