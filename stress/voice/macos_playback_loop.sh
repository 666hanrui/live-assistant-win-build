#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILE="${1:-quick}"
ROUNDS="${2:-1}"
GAP="${3:-2}"

"$ROOT/.venv/bin/python" "$ROOT/scripts/voice_audio_runner.py" play-mac --profile "$PROFILE" --rounds "$ROUNDS" --gap-seconds "$GAP"

echo "Playback done."
