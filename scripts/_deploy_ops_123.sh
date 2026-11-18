#!/bin/sh
set -u
mkdir -p /root/qm-src-123
cd /root/qm-src-123
tar xzf /root/qingmeng-src-1.9.23.tar.gz
attempt=1
ok=0
while [ $attempt -le 4 ]; do
  if docker build --network host -t qingmeng/qingmeng-gpt:1.9.23 . > /root/qm-src-123/build.log 2>&1; then ok=1; break; fi
  echo "build attempt $attempt failed"; tail -5 /root/qm-src-123/build.log; attempt=$((attempt + 1)); sleep 10
done
[ $ok -eq 1 ] || { DOCKER_BUILDKIT=0 docker build --network host -t qingmeng/qingmeng-gpt:1.9.23 . > /root/qm-src-123/build-classic.log 2>&1 || { tail -20 /root/qm-src-123/build-classic.log; exit 1; }; }

# 不改动 compose 的 .env，只替换 tag：先备份旧 tag，再让旧 tag 指向新镜像
OLD_IMAGE=$(grep '^QINGMENG_IMAGE=' /www/server/panel/data/compose/qingmeng2api/.env | cut -d= -f2)
echo "old_image=$OLD_IMAGE"
docker tag "$OLD_IMAGE" "${OLD_IMAGE}-backup-20261119d" 2>/dev/null || true
docker tag qingmeng/qingmeng-gpt:1.9.23 "$OLD_IMAGE"

cd /www/server/panel/data/compose/qingmeng2api
docker compose -f docker-compose.yaml up -d --force-recreate >/dev/null 2>&1
i=0
while [ $i -lt 90 ]; do
  if curl -fsS -m 3 http://127.0.0.1:3100/version >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 2
done
echo -n "version: "; curl -s http://127.0.0.1:3100/version; echo
docker ps --filter name=qingmeng-gpt --format '{{.Names}} | {{.Status}}'
docker logs --tail 20 qingmeng-gpt 2>&1 | tail -8
