#!/usr/bin/env bash
# 서버에서 실행한다. 최신 main 을 받아 다시 빌드하고 띄운다.
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose up -d --build --wait
docker image prune -f
