#!/usr/bin/env bash
set -euo pipefail
python3 -m pip install -r requirements.txt
exec uvicorn backend.app:app --reload --port 8000
