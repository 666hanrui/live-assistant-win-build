#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILE="${1:-quick}"

"$ROOT/.venv/bin/python" "$ROOT/scripts/voice_audio_runner.py" generate-mac --profile "$PROFILE"

echo "Done. Audio files at: $ROOT/stress/voice/audio_mac"
