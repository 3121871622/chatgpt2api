#!/usr/bin/env bash
#
# 倾梦GPT绘画企业级百万号池系统 · 一键部署
#
# 本地构建镜像 → 传输到服务器 → 启动 compose → 健康检查。
# 适用于服务器无法访问 npm/pypi（DNS 或网络受限）的场景：镜像在本地/CI 构建。
#
# 用法：
#   ./deploy/deploy-qingmeng.sh root@your-server-ip
#   ./deploy/deploy-qingmeng.sh root@host --tag 1.0.0 --port 3000
#
# 前置：本机已安装 docker；已配置 SSH 免密（或手动输入密码）。
set -euo pipefail

TARGET="${1:-}"
shift || true

TAG="1.9.8"
REMOTE_DIR="/opt/qingmeng"
HOST_PORT="3000"
IMAGE="qingmeng/qingmeng-gpt"

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --port) HOST_PORT="$2"; shift 2 ;;
    --dir) REMOTE_DIR="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ -z "${TARGET}" ]; then
  echo "用法：$0 <user@host> [--tag 1.0.0] [--port 3000] [--dir /opt/qingmeng]" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_REF="${IMAGE}:${TAG}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "[1/6] 构建镜像 ${IMAGE_REF}（--network host 规避构建期 DNS 问题）"
docker build --network host -t "${IMAGE_REF}" "${ROOT_DIR}"

echo "[2/6] 导出镜像"
docker save "${IMAGE_REF}" | gzip -1 > "${WORK_DIR}/image.tar.gz"

echo "[3/6] 上传镜像与部署文件到 ${TARGET}:${REMOTE_DIR}"
ssh "${TARGET}" "mkdir -p '${REMOTE_DIR}/data'"
scp "${WORK_DIR}/image.tar.gz" "${TARGET}:${REMOTE_DIR}/image.tar.gz"
scp "${ROOT_DIR}/docker-compose.qingmeng.yml" "${TARGET}:${REMOTE_DIR}/docker-compose.yml"
if [ -f "${ROOT_DIR}/config.json" ]; then
  scp "${ROOT_DIR}/config.json" "${TARGET}:${REMOTE_DIR}/config.json"
fi

echo "[4/6] 服务器加载镜像并生成 .env"
ssh "${TARGET}" bash -s <<REMOTE
set -euo pipefail
cd '${REMOTE_DIR}'
gunzip -c image.tar.gz | docker load
rm -f image.tar.gz
if [ ! -f .env ]; then
  AUTH_KEY="\$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > .env <<ENV
QINGMENG_AUTH_KEY=\${AUTH_KEY}
QINGMENG_PORT=${HOST_PORT}
QINGMENG_IMAGE=${IMAGE_REF}
TZ=Asia/Shanghai
ENV
  chmod 600 .env
  echo "已生成 .env，管理密钥：\${AUTH_KEY}"
else
  echo ".env 已存在，保留原配置"
fi
if grep -q '^QINGMENG_IMAGE=' .env; then
  sed -i "s|^QINGMENG_IMAGE=.*|QINGMENG_IMAGE=${IMAGE_REF}|" .env
fi
REMOTE

echo "[5/6] 启动容器"
ssh "${TARGET}" bash -s <<REMOTE
set -euo pipefail
cd '${REMOTE_DIR}'
docker compose -f docker-compose.yml up -d --remove-orphans
REMOTE

echo "[6/6] 健康检查"
ssh "${TARGET}" bash -s <<REMOTE
set -euo pipefail
cd '${REMOTE_DIR}'
for i in \$(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/version" >/dev/null 2>&1; then
    echo "服务已就绪：http://127.0.0.1:${HOST_PORT}/version"
    curl -sS "http://127.0.0.1:${HOST_PORT}/version"; echo
    docker compose -f docker-compose.yml ps
    exit 0
  fi
  sleep 2
done
echo "健康检查超时，最近日志：" >&2
docker compose -f docker-compose.yml logs --tail=80 app >&2
exit 1
REMOTE
