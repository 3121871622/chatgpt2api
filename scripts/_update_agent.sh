#!/bin/sh
# 一键更新宿主机代理。
#
# 控制台「更新中心」把请求写入 <data>/update/request.json，cron 每分钟运行本脚本：
#   校验版本 -> docker pull ghcr.io/<repo>:<version> -> 备份并重打旧镜像 tag
#   -> docker compose up -d --force-recreate -> 回写 state.json（pulling/applying/done/failed）
# 应用容器不挂载 docker.sock，所有宿主机动作都由本脚本以 root 执行。
set -u

DATA=/www/dk_project/dk_app/chatgpt2api/data
UPD="$DATA/update"
STATE="$UPD/state.json"
REQ="$UPD/request.json"
LOG="$UPD/agent.log"
COMPOSE_DIR=/www/server/panel/data/compose/qingmeng2api
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yaml"
IMAGE_REPO=ghcr.io/3121871622/chatgpt2api
CONTAINER=qingmeng-gpt

mkdir -p "$UPD"
chown 10001:10001 "$UPD" 2>/dev/null || true

now() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s %s\n' "$(now)" "$*" >>"$LOG"; }

write_state() {
    # $1=phase $2=target $3=current $4=message $5=finished_at
    tmp="$STATE.tmp"
    printf '{"phase":"%s","target_version":"%s","current_version":"%s","message":"%s","updated_at":"%s","finished_at":"%s"}\n' \
        "$1" "$2" "$3" "$4" "$(now)" "$5" >"$tmp" && mv "$tmp" "$STATE"
}

current_version() {
    docker exec "$CONTAINER" cat /opt/qingmeng/VERSION 2>/dev/null | tr -d '\r\n'
}

CUR=$(current_version)

if [ ! -f "$REQ" ]; then
    write_state idle "" "$CUR" "等待更新请求" ""
    exit 0
fi

# concurrency guard: two cron ticks must not pull at the same time
exec 9>"$UPD/.agent.lock"
if ! flock -n 9; then
    exit 0
fi

VER=$(sed -n 's/.*"target_version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$REQ" | head -1)
if [ -z "$VER" ]; then
    write_state failed "" "$CUR" "更新请求缺少合法的 target_version" "$(now)"
    mv "$REQ" "$REQ.invalid" 2>/dev/null || rm -f "$REQ"
    log "invalid update request"
    exit 1
fi

log "accept update request target=$VER current=$CUR"
mv "$REQ" "$REQ.processing"
write_state pulling "$VER" "$CUR" "拉取镜像 $IMAGE_REPO:$VER" ""

if ! docker pull "$IMAGE_REPO:$VER" >>"$LOG" 2>&1; then
    write_state failed "$VER" "$CUR" "docker pull 失败：GHCR 镜像可能尚未发布，稍后重试" "$(now)"
    mv "$REQ.processing" "$REQ.failed" 2>/dev/null || rm -f "$REQ.processing"
    log "docker pull failed target=$VER"
    exit 1
fi

write_state applying "$VER" "$CUR" "重打镜像标签并重启容器" ""

OLD_IMAGE=$(grep '^QINGMENG_IMAGE=' "$COMPOSE_DIR/.env" 2>/dev/null | cut -d= -f2)
if [ -n "$OLD_IMAGE" ]; then
    docker tag "$OLD_IMAGE" "${OLD_IMAGE}-preupdate-$(date +%Y%m%d-%H%M%S)" >>"$LOG" 2>&1 || true
    docker tag "$IMAGE_REPO:$VER" "$OLD_IMAGE" >>"$LOG" 2>&1 || true
fi
docker tag "$IMAGE_REPO:$VER" "qingmeng/qingmeng-gpt:$VER" >>"$LOG" 2>&1 || true

( cd "$COMPOSE_DIR" && docker compose -f "$COMPOSE_FILE" up -d --force-recreate ) >>"$LOG" 2>&1

i=0
while [ $i -lt 90 ]; do
    if curl -fsS -m 3 http://127.0.0.1:3100/version >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 2
done
sleep 2

NEW=$(current_version)
if [ "$NEW" = "$VER" ]; then
    write_state done "$VER" "$NEW" "更新完成：$CUR -> $NEW" "$(now)"
    log "update done $CUR -> $NEW"
    rm -f "$REQ.processing"
else
    write_state failed "$VER" "$NEW" "容器版本仍为 $NEW（目标 $VER），请检查 $COMPOSE_DIR 与 /data/update/agent.log" "$(now)"
    log "update failed target=$VER current=$NEW"
    mv "$REQ.processing" "$REQ.failed" 2>/dev/null || true
fi
