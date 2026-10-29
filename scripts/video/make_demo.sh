#!/usr/bin/env bash
# Reproducible demo-video draft: TTS -> start app (Sandbox + Claude) -> record -> assemble.
# Needs: pip install playwright piper-tts; `playwright install ffmpeg` (+ chromium or `playwright install chromium`); system ffmpeg.
# Env: PAYPAL_MODE=sandbox + Sandbox credentials (or PAYPAL_AUTH=proxy), Anthropic access
# (ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL). Set DEMO_AGENT_MODE=offline to record without an AI key.
set -euo pipefail
cd "$(dirname "$0")/../.."
BUILD=${BUILD:-build/video}; PORT=${PORT:-8011}; VOICE=${VOICE:-}
mkdir -p "$BUILD"; rm -f "$BUILD/demo.db"
python3 scripts/video/tts.py ${VOICE:+--voice "$VOICE"} --out "$BUILD"
APPROVER_TOKEN=demo DB_PATH="$BUILD/demo.db" PUBLIC_BASE_URL="http://localhost:$PORT" \
  PAYPAL_MODE=${PAYPAL_MODE:-sandbox} AGENT_MODE=${DEMO_AGENT_MODE:-claude} \
  python3 -m uvicorn app.main:app --port "$PORT" > "$BUILD/server.log" 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 30); do curl -sf "localhost:$PORT/health" >/dev/null && break; sleep 0.5; done
python3 scripts/video/record_demo.py --base "http://localhost:$PORT" --durations "$BUILD/durations.json" --out "$BUILD"
python3 scripts/video/assemble.py --build "$BUILD" --out "${OUT:-docs/demo_draft.mp4}"
