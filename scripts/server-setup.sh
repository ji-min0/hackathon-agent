#!/usr/bin/env bash
# 새 Ubuntu 서버(AWS EC2, NCP 등)에 Docker 를 설치하고, 메모리가 작으면 스왑을 만든다.
# 서버에서 한 번만 실행한다:  bash scripts/server-setup.sh
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "== Docker 설치"
  curl -fsSL https://get.docker.com | sudo sh
  # 다시 로그인하면 sudo 없이 docker 를 쓸 수 있다.
  sudo usermod -aG docker "$USER"
else
  echo "== Docker 이미 설치됨: $(docker --version)"
fi

# 메모리 2GB 미만(AWS 무료 범위 t2/t3.micro 등)이면 이미지 빌드 중 메모리가 모자랄 수 있다.
total_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$total_mb" -lt 2048 ] && ! sudo swapon --show | grep -q /swapfile; then
  echo "== 메모리 ${total_mb}MB: 스왑 2GB 생성"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "== 완료. 'exit' 후 다시 접속한 뒤 docs/deploy.md 의 다음 단계를 진행하세요."
