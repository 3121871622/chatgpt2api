#!/bin/sh
#
# 倾梦GPT绘画企业级百万号池系统 · 容器入口
#
# 负责三件事：
#   1. 容器网络自检：部分机房容器内 DNS 解析异常（resolv.conf 缺失 / 上游 DNS 不可达），
#      这里检测并按需修复，随后强制 IPv4 优先，避免 IPv6 黑洞导致的长时间等待。
#   2. 运行目录准备：data / config.json / 数据库位置。
#   3. 执行真正的启动命令。
set -eu

log() { printf '[qingmeng] %s\n' "$*"; }

DATA_DIR="${DATA_DIR:-/data}"
PUBLIC_DNS="${QINGMENG_DNS_FALLBACK:-1.1.1.1 8.8.8.8 223.5.5.5}"

# ---------------------------------------------------------------- DNS 自检
dns_ok() {
  # getent 只做解析，不产生外网依赖；没有 getent 时退回 curl。
  if command -v getent >/dev/null 2>&1; then
    getent hosts registry.npmjs.org >/dev/null 2>&1 && return 0
    getent hosts pypi.org >/dev/null 2>&1 && return 0
    return 1
  fi
  curl -s -m 5 -o /dev/null https://pypi.org/simple/ 2>/dev/null
}

repair_dns() {
  resolv=/etc/resolv.conf
  if [ ! -w "${resolv}" ]; then
    log "resolv.conf 不可写，跳过 DNS 修复（请在 compose 中配置 dns:，见 docker-compose.qingmeng.yml）"
    return 0
  fi
  log "检测到容器 DNS 解析异常，追加公共 DNS 作为兜底：${PUBLIC_DNS}"
  for server in ${PUBLIC_DNS}; do
    grep -q "nameserver ${server}" "${resolv}" 2>/dev/null || echo "nameserver ${server}" >> "${resolv}"
  done
  if ! grep -q '^options' "${resolv}" 2>/dev/null; then
    echo "options timeout:2 attempts:3 single-request-reopen" >> "${resolv}"
  fi
}

if ! dns_ok; then
  repair_dns || true
  if dns_ok; then
    log "DNS 已恢复"
  else
    log "DNS 仍不可用：容器可离线启动，但上游请求会失败。请检查宿主机 /etc/resolv.conf 与安全组。"
  fi
else
  log "DNS 正常"
fi

# IPv4 优先，绕开部分机房 IPv6 无出口造成的连接悬挂。
if [ -w /etc/gai.conf ] && ! grep -q '^precedence ::ffff:0:0/96' /etc/gai.conf 2>/dev/null; then
  echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf 2>/dev/null || true
fi

# ------------------------------------------------------------- 运行目录
RUN_UID=10001
RUN_GID=10001
mkdir -p "${DATA_DIR}" 2>/dev/null || true

fix_permissions() {
  if [ "$(id -u)" != "0" ]; then
    if [ ! -w "${DATA_DIR}" ]; then
      log "数据目录 ${DATA_DIR} 不可写：请确认卷挂载权限（UID ${RUN_UID}）"
    fi
    return 0
  fi

  needs_fix=0
  owner=$(stat -c '%u' "${DATA_DIR}" 2>/dev/null || echo "")
  if [ "${owner}" != "${RUN_UID}" ]; then
    needs_fix=1
  elif find "${DATA_DIR}" -mindepth 1 -maxdepth 1 ! -user "${RUN_UID}" -print -quit 2>/dev/null | grep -q .; then
    needs_fix=1
  fi

  if [ "${needs_fix}" = "1" ]; then
    log "修正数据目录权限：${DATA_DIR} -> ${RUN_UID}:${RUN_GID}"
    chown -R "${RUN_UID}:${RUN_GID}" "${DATA_DIR}" 2>/dev/null \
      || log "chown 失败：宿主机文件系统可能不允许容器修改属主"
    chmod -R u+rwX,g+rwX "${DATA_DIR}" 2>/dev/null || true
  fi
  chmod 775 "${DATA_DIR}" 2>/dev/null || true
}

fix_permissions

run_as_app() {
  if [ "$(id -u)" = "0" ] && command -v gosu >/dev/null 2>&1; then
    exec gosu "${RUN_UID}:${RUN_GID}" "$@"
  fi
  exec "$@"
}

# 兼容旧的 /app 卷布局：老部署把数据放在 /app/data，这里做一次桥接。
if [ -d /app/data ] && [ "${DATA_DIR}" = "/data" ] && [ -z "$(ls -A "${DATA_DIR}" 2>/dev/null)" ]; then
  log "检测到历史 /app/data，复制到 ${DATA_DIR}"
  cp -a /app/data/. "${DATA_DIR}/" 2>/dev/null || true
fi

# --------------------------------------------------------- 自动迁移旧数据
# 在 uvicorn 启动前完成，保证只有一个进程写库。目标库已有账号时不会覆盖。
if [ "${QINGMENG_AUTO_MIGRATE:-1}" != "0" ]; then
  MIGRATE_SCRIPT=/opt/qingmeng/scripts/auto_migrate_legacy.py
  if [ -f "${MIGRATE_SCRIPT}" ]; then
    if [ -x /opt/qingmeng/.venv/bin/python ]; then
      if [ "$(id -u)" = "0" ] && command -v gosu >/dev/null 2>&1; then
        gosu "${RUN_UID}:${RUN_GID}" /opt/qingmeng/.venv/bin/python "${MIGRATE_SCRIPT}" \
          || log "自动迁移失败，继续启动（可稍后用 migrate_legacy_data.py 手工处理）"
      else
        /opt/qingmeng/.venv/bin/python "${MIGRATE_SCRIPT}" \
          || log "自动迁移失败，继续启动（可稍后用 migrate_legacy_data.py 手工处理）"
      fi
    else
      uv run python "${MIGRATE_SCRIPT}" || log "自动迁移失败，继续启动"
    fi
  fi
fi

log "启动：DATA_DIR=${DATA_DIR} 版本=$(cat /opt/qingmeng/VERSION 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- Go 网关
# 仅当 GATEWAY_TOKEN 配置时启动。Go 网关监听 127.0.0.1:8081（可用
# GATEWAY_ADDR 覆盖），Python 应用通过本地 HTTP 调用它完成账号租约。
# DATABASE_URL 存在时网关也持久化账号到 PostgreSQL。
if [ -n "${GATEWAY_TOKEN:-}" ]; then
  if [ -x /usr/local/bin/qingmeng-gateway ]; then
    log "启动 Go 网关（GATEWAY_ADDR=${GATEWAY_ADDR:-127.0.0.1:8081}）"
    if [ "$(id -u)" = "0" ] && command -v gosu >/dev/null 2>&1; then
      gosu "${RUN_UID}:${RUN_GID}" /usr/local/bin/qingmeng-gateway &
    else
      /usr/local/bin/qingmeng-gateway &
    fi
    # 给网关一点时间完成 TCP 绑定和 PG 连接，Python 客户端会在第一次调用时
    # 自动降级到本地索引，所以这里不需要严格等待。
    sleep 1
  else
    log "警告：未找到 qingmeng-gateway 二进制，跳过 Go 网关启动"
  fi
fi

run_as_app "$@"
