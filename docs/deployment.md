# 部署与升级手册

Status: current

适用版本：v1.2.4（镜像 `qingmeng/qingmeng-gpt:1.2.4`）。

本文覆盖生产部署、宝塔面板、权限修正、旧库网页导入、升级回滚和常见故障。
试运营期每日检查、调度与告警处置见 [runbook.md](runbook.md)。

## 1. 运行形态与当前事实

| 形态 | 编排入口 | 数据位置 | 说明 |
| --- | --- | --- | --- |
| 精简 Compose（推荐） | `docker-compose.qingmeng.yml` | `./data` | 无 `config.json` 挂载、无 legacy 挂载、`QINGMENG_AUTO_MIGRATE=0`；宿主端口默认 **3100** |
| 默认 Compose（兼容旧部署） | `docker-compose.yml` | `./data` + `./config.json` | 仍带旧挂载和自动迁移默认值；宿主端口默认 **3000**；新部署不要混用 |
| 裸机 | `uv run uvicorn main:app` | `QINGMENG_DATA_DIR` 或 `./data` | 需要自行保证运行用户对数据目录可写 |
| 开发 | `uv run uvicorn main:app --reload` | 同裸机 | 前端另开 `web-vue` 开发服务 |

镜像名固定带 `qingmeng`：`qingmeng/qingmeng-gpt:<version>`。生产环境固定到不可变版本号，
不要使用 `latest`。精简 Compose 与默认 Compose 的默认端口不同，访问前以
`docker compose port app 80` 的结果为准：

| 入口 | 默认宿主端口 |
| --- | --- |
| `docker-compose.qingmeng.yml` / `.env.qingmeng.example` | `3100` |
| `docker-compose.yml` | `3000` |
| `deploy/deploy-qingmeng.sh` | `3000`（可用 `--port 3100` 覆盖） |

容器内应用监听 **80**，健康检查也只访问容器内 80：

```text
GET /version  ->  {"version":"1.2.4"}
```

### 1.1 数据目录

容器内默认 `DATA_DIR=/data`，精简 Compose 只挂载 `./data:/data`：

| 路径 | 内容 |
| --- | --- |
| `/data/chatgpt2api.db` | SQLite 主库（WAL 模式，另有 `-wal` / `-shm`） |
| `/data/backups/` | 本地自动备份 `chatgpt2api-YYYYMMDD-HHMMSS.db` |
| `/data/imports/` | 网页上传的旧库文件，导入后不会自动删除 |
| `/data/debug-logs/` | 调试日志 `debug-*.log`（JSON Lines） |
| `/data/images/`、`/data/image_thumbnails/` | 图片与缩略图 |
| `/data/files/` | 可编辑文件任务产物 |
| `/data/legacy_sync.json` 等 | 旧项目目录同步、告警、本地备份的运行配置与状态 |

`config.json` 在当前版本只作为 **管理员密钥的旧版引导兜底**：
`QINGMENG_AUTH_KEY` > `CHATGPT2API_AUTH_KEY` > `config.json` 的 `auth-key`。
核心业务设置（账号、并发、图片、代理等）存数据库；本地备份、调试日志、旧库同步、告警等
运维开关存 `data/` 下的 JSON。网页「设置」是这些配置的主要入口。
精简 Compose 不挂载 `config.json`，因此不需要预先创建该文件。

## 2. 环境变量

只有引导必需项留在 `.env`，并发、调度、图片、日志、备份等均在网页配置；唯一例外是
`QINGMENG_MAX_REQUEST_CONCURRENCY`（准入上限覆盖项）。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `QINGMENG_AUTH_KEY` | 是 | 管理员 Bearer 密钥；Compose 使用 `:?` 校验，空值会在启动前报错 |
| `QINGMENG_BASE_URL` | 否 | 对外访问地址，用于生成图片公网链接；HTTPS 域名部署时填写 |
| `QINGMENG_DATA_DIR` | 否 | 数据目录，容器内 `/data` |
| `DATABASE_URL` | 否 | 留空使用 SQLite；PostgreSQL 形如 `postgresql://user:pass@host:5432/db` |
| `TZ` | 否 | 时区，默认 `Asia/Shanghai` |
| `QINGMENG_PORT` | 否 | 宿主端口；精简 Compose 默认 3100 |
| `QINGMENG_IMAGE` | 否 | 镜像名与版本，建议显式固定 |
| `QINGMENG_AUTO_MIGRATE` | 否 | 精简 Compose 固定 `0`；`1` 仅在一次性自动迁移时使用 |
| `QINGMENG_MAX_REQUEST_CONCURRENCY` | 否 | 准入上限覆盖值，默认按 CPU 核数 ×16 推导，范围 64–4096 |
| `QINGMENG_DNS_FALLBACK` | 否 | 入口脚本探测 DNS 失败时追加的公共 DNS，默认 `1.1.1.1 8.8.8.8 223.5.5.5` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | 否 | 仅启用内置 PostgreSQL 服务时需要 |

兼容：`CHATGPT2API_AUTH_KEY`、`CHATGPT2API_BASE_URL` 仍可读，`QINGMENG_*` 优先。
常规业务并发/线程在「设置 → 基础配置 / 智能调度与维护」中维护；只有准入上限
`QINGMENG_MAX_REQUEST_CONCURRENCY` 是环境变量覆盖项。

## 3. 精简 Compose 部署（标准流程）

### 3.1 前置条件

- Docker 24+ 与 Docker Compose v2：`docker compose version`；
- 建议至少 2 核 / 4 GB；百万级号池的容量参考见 [runbook.md](runbook.md)；
- 数据盘留出数据库体积的 2 倍以上；SQLite 不要放在 NFS/CIFS 上；
- 已放行宿主端口（默认 3100）或准备好反向代理。

如果面板只提供旧版 `docker-compose`（v1），先升级/安装 Compose v2。当前编排文件使用
`services:` 语法，v1 可能直接报格式错误。

### 3.2 准备目录与 `.env`

以下 `/opt/qingmeng` 是示例部署目录，按实际路径替换：

```bash
DEPLOY_DIR=/opt/qingmeng
mkdir -p "$DEPLOY_DIR/data"
cd "$DEPLOY_DIR"

cp /path/to/docker-compose.qingmeng.yml ./docker-compose.qingmeng.yml
cp /path/to/.env.qingmeng.example ./.env

# 生成 64 位十六进制管理员密钥（只在本机执行）
sed -i "s|^QINGMENG_AUTH_KEY=.*|QINGMENG_AUTH_KEY=$(openssl rand -hex 32)|" .env
chmod 600 .env
```

编辑 `.env`，至少确认：

```dotenv
QINGMENG_AUTH_KEY=<32 字节以上随机串>
QINGMENG_IMAGE=qingmeng/qingmeng-gpt:1.2.4
QINGMENG_PORT=3100
QINGMENG_BASE_URL=https://your-domain.example
TZ=Asia/Shanghai
```

> 安全提醒：当前版本只拒绝空管理员密钥，不会自动拒绝 `.env.qingmeng.example` 里的
> `change-me-please` 等示例值，也不强制最小长度。必须人工确认已替换为随机串，否则控制台
> 等同于公开。对外开放前还必须处置调试导出包的敏感信息和旧库上传的来源限制。

不要预先创建 `config.json`，也不要把旧数据目录挂成 `/legacy`。精简 Compose 的迁移入口
是网页上传，见第 4 节。

### 3.3 启动与首次日志

本地已有镜像时：

```bash
cd "$DEPLOY_DIR"
docker compose -f docker-compose.qingmeng.yml up -d --no-build
docker compose -f docker-compose.qingmeng.yml ps
docker compose -f docker-compose.qingmeng.yml logs --tail=120 app
```

没有本地镜像、需要首次构建时，去掉 `--no-build`，或在启动前 `docker load -i` 离线镜像。

首次启动的日志应按顺序出现：

```text
qingmeng-gpt  | [qingmeng] DNS 正常
qingmeng-gpt  | [qingmeng] 修正数据目录权限：/data -> 10001:10001
qingmeng-gpt  | [qingmeng] 启动：DATA_DIR=/data 版本=1.2.4
qingmeng-gpt  | INFO:     Started server process [1]
qingmeng-gpt  | INFO:     Waiting for application startup.
qingmeng-gpt  | INFO:     Application startup complete.
qingmeng-gpt  | INFO:     Uvicorn running on http://0.0.0.0:80
```

判读：

| 日志 | 含义 |
| --- | --- |
| `DNS 正常` | 容器 DNS 探测通过 |
| `DNS 已恢复` | 入口脚本追加了公共 DNS 后恢复 |
| `DNS 仍不可用` | 服务可离线启动，但上游请求会失败；按 8.3 处理 |
| `修正数据目录权限：...` | 宿主目录属主不是 10001，入口以 root 修正后降权；首次启动出现一次正常 |
| 没有权限修正行 | 目录属主已经是 10001，正常跳过 |
| `chown 失败` | 宿主文件系统不允许容器改属主；按 3.4 手工处理 |
| `数据目录 /data 不可写` | 入口不是以 root 运行（例如 compose 里覆盖了 `user:`）；按 3.4 处理 |
| `[auto-migrate]` | 精简 Compose 不应出现；出现说明 `QINGMENG_AUTO_MIGRATE` 不是 0 |

首次启动会初始化数据库、硬件档位默认值和统计投影，通常 10–30 秒。精简 Compose 的
健康检查 `start_period` 是 60 秒；这段时间内 `starting` 是正常的。若 `data/` 很大，
入口的递归 `chown` 可能更久，先看日志，不要在中途反复重启。

验证：

```bash
curl -fsS http://127.0.0.1:3100/version
docker compose -f docker-compose.qingmeng.yml ps
```

浏览器访问 `http://<服务器IP>:3100`，用 `QINGMENG_AUTH_KEY` 登录。`ps` 显示
`healthy`、`/version` 返回 1.2.4 即启动成功。

### 3.4 数据目录权限：自动修正与仍失败时

镜像内应用以 **UID/GID 10001** 运行。自 1.1.7.3 起，容器入口以 root 启动，先检查
`/data`，必要时执行：

```text
chown -R 10001:10001 /data
chmod -R u+rwX,g+rwX /data
chmod 775 /data
```

然后用 `gosu 10001:10001` 降权启动 uvicorn。应用进程仍然是 10001，安全边界不变。
入口只在以下情况修正：

- `/data` 本身属主不是 10001；或
- `/data` 顶层任意一个条目属主不是 10001。

**注意边界**：入口的检测只看顶层条目，不逐层判断；一旦触发，`chown` 是递归的。若顶层
目录已经是 10001，但更深层的 `images/`、`files/` 或 `chatgpt2api.db-wal` 是 root 属主，
入口会跳过自动修正，应用仍可能在写该文件时报错。此时需要手工 `chown -R`。

#### 3.4.1 仍失败时的排查顺序

在宿主机执行（示例目录 `/opt/qingmeng`）：

```bash
cd /opt/qingmeng

# 1) 宿主机属主/权限；注意 db 的 -wal / -shm
stat -c '%u:%g %a %n' data data/chatgpt2api.db data/chatgpt2api.db-wal 2>/dev/null

# 2) 容器内应用进程的真实 UID（docker compose exec 默认可能是 root，不要只看 id）
docker compose -f docker-compose.qingmeng.yml exec app sh -c 'grep ^Uid /proc/1/status'

# 3) 以 10001 身份做写测试
docker compose -f docker-compose.qingmeng.yml exec -u 10001 app sh -c \
  'touch /data/.write-test && rm /data/.write-test && echo WRITABLE'

# 4) 磁盘与 inode
docker compose -f docker-compose.qingmeng.yml exec app sh -c 'df -h /data; df -i /data'

# 5) 挂载选项；ro 表示只读
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS -T /opt/qingmeng/data 2>/dev/null || mount | grep ' /opt/qingmeng/data '
```

常见根因与处理：

| 根因 | 证据 | 处理 |
| --- | --- | --- |
| 宿主目录 root 属主，且 chown 成功 | 日志有 `修正数据目录权限`，写测试通过 | 无需处理，后续启动会跳过 |
| 宿主目录 root 属主，但 chown 失败 | 日志 `chown 失败`；写测试失败 | 在宿主机 `chown -R 10001:10001 data`，再 `chmod -R u+rwX,g+rwX data` |
| 只有深层文件属主错误 | 顶层已 10001，db 或 images 仍是 root | `chown -R 10001:10001` 针对整个 `data`，不要只改顶层 |
| `data` 是只读挂载 | `findmnt` 的 `OPTIONS` 含 `ro` | 去掉 `:ro`，或换可写目录 |
| NFS/CIFS 不允许容器改属主 | `chown` 失败；`findmnt` 显示 nfs/cifs | 改为本地磁盘；或在挂载端使用 `anonuid=10001,anongid=10001`；SQLite 不建议放网络盘 |
| SELinux 拦截 | 宿主机 `getenforce` 为 Enforcing，容器日志有 permission denied | 给数据目录打容器标签（如 `chcon -Rt container_file_t data`），或按发行版策略处理 |
| rootless Docker | 容器内 root 是宿主机普通用户，chown 映射失败 | 在宿主机按 rootless 映射预置属主，或改用 rootful Docker |
| 磁盘满 / inode 满 | `df -h` 或 `df -i` 满 | 清理 `data/backups`、`data/imports`、旧日志/图片或扩容 |
| compose 覆盖了 `user:` | 日志 `数据目录 /data 不可写`，`/proc/1/status` 不是 10001 | 去掉 `user:` 覆盖；如必须固定用户，先在宿主机预置 10001 可写 |

手工兜底（确认是权限问题后再执行）：

```bash
cd /opt/qingmeng
docker compose -f docker-compose.qingmeng.yml stop app
chown -R 10001:10001 data
chmod -R u+rwX,g+rwX data
chmod 775 data
docker compose -f docker-compose.qingmeng.yml up -d
```

不要对 `/www`、`/opt` 等上级目录做递归 `chown`。宝塔文件管理器、面板备份任务或
`www` 用户操作可能把属主改回 root；再次启动时入口会修正顶层，若仍有深层文件问题按上表处理。

### 3.5 宝塔面板部署

宝塔的入口名称随版本变化，按当前面板实际菜单操作；核心是把精简 Compose 作为项目编排文件。

1. 在宝塔「Docker」中安装/启用 Docker，并确认终端里 `docker compose version` 可用。
2. 新建部署目录（示例 `/www/wwwroot/qingmeng`），确保在 Linux 数据盘上，不要放在
   Windows 共享目录或网络盘。
3. 上传 `docker-compose.qingmeng.yml` 与 `.env` 到该目录；创建 `data/`。
4. 如果面板的编排功能只识别 `docker-compose.yml`，把精简文件复制为该名称：
   ```bash
   cp docker-compose.qingmeng.yml docker-compose.yml
   ```
   注意不要把仓库根目录那份旧的 `docker-compose.yml` 一起放进来，否则会带到
   `config.json` 挂载和自动迁移默认值。
5. 在宝塔终端启动：
   ```bash
   cd /www/wwwroot/qingmeng
   docker compose up -d --no-build
   docker compose ps
   docker compose logs --tail=120 app
   ```
6. 在「安全」中放行宿主端口（默认 3100）；云厂商安全组同样放行。只在本机验证通过、
   外网打不开时，优先查这两层。
7. 需要域名/HTTPS 时，在宝塔建站点并反代到 `127.0.0.1:3100`，见 3.6。

宝塔相关注意：

- 面板文件管理器创建 `data/` 时通常是 root 属主，1.1.7.3+ 首次启动会自动修正；等日志出现
  `修正数据目录权限` 后再判断失败。
- 面板显示 `unhealthy` 不一定代表容器退出；先看 `docker compose ps` 的状态和启动日志，
  首次启动 60 秒内属正常窗口。
- 宝塔的计划任务如果直接 `tar` 运行中的 `data/`，可能拿到不一致的 SQLite 快照。备份请用
  应用内「本地自动备份 / R2 备份」，或停容器后再打包，见第 6 节。
- 面板的 Docker 功能通常需要 root 权限；不要把面板账号给无关人员。

### 3.6 反向代理与 HTTPS

宝塔站点 → 反向代理到 `http://127.0.0.1:3100`。Nginx 关键配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:3100;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE / 长请求：关闭缓冲，拉长读超时
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    # 旧库网页上传：按实际需要放大；否则 Nginx 会先返回 413
    client_max_body_size 2048m;
    proxy_request_buffering off;
}
```

- `QINGMENG_BASE_URL` 填最终对外地址（含 `https://`），否则图片链接可能指向容器内地址。
- 反向代理只代理到宿主 `127.0.0.1:3100`；不要把 3100 直接暴露到公网再套代理。
- 图片生成/对话使用 SSE，`proxy_buffering off` 和足够的 `proxy_read_timeout` 必须保留。
- 如果前面还有 Cloudflare，注意其套餐的请求体上限（常见 100 MB）和 100 秒级超时：
  2 GB 旧库上传应走直连或临时绕过 CDN，不要在上传失败时只查应用日志。

### 3.7 PostgreSQL（可选）

SQLite 适合单机、约 5 万账号以内；更大规模建议 PostgreSQL。精简 Compose 没有内置
PostgreSQL 服务，推荐使用外部/托管 PostgreSQL，或自行在编排中增加服务。

```dotenv
DATABASE_URL=postgresql://qingmeng:强密码@postgres.example:5432/qingmeng
```

```bash
docker compose -f docker-compose.qingmeng.yml up -d
```

注意：

- 精简 Compose 与默认 Compose 的 PostgreSQL 服务不同：默认 `docker-compose.yml` 带有
  `postgres` profile，但仍保留旧 `config.json` 挂载和 `QINGMENG_AUTO_MIGRATE` 默认值，
  只适合兼容旧部署；新部署建议外部 PG。
- 首次连接会自动建表；切换数据库前先导出/备份账号。
- PostgreSQL 下应用内「本地自动备份」会跳过，必须使用 R2 备份或 `pg_dump`。
- 旧 SQLite 迁到 PostgreSQL 用第 4.5 节的一次性脚本，并带 `--database-url`；当前该路径只写
  账号与授权密钥，设置/代理/账号组/日志仍需重建或另行迁移。
- 生产备份示例：
  ```bash
  pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" > qingmeng-$(date +%F).pgdump
  ```

### 3.8 DNS 解析异常

机房容器常见两类问题：`/etc/resolv.conf` 被覆盖、上游 DNS 不可达。症状包括
`Temporary failure in name resolution`、`Name or service not known`、`server misbehaving`，
以及构建阶段 `npm` / `pip` / `docker pull` 全部超时。

精简 Compose 已内置：

```yaml
dns:
  - 223.5.5.5
  - 1.1.1.1
dns_opt:
  - timeout:2
  - attempts:3
```

入口脚本还会在启动时探测 DNS；异常时向 `/etc/resolv.conf` 追加
`QINGMENG_DNS_FALLBACK` 中的公共 DNS，并写入超时参数，然后强制 IPv4 优先，避免 IPv6
无出口导致连接悬挂。

如果所有容器都出现解析失败，而不只是本服务，可在 `/etc/docker/daemon.json` 中合并
Docker 守护进程级 DNS，然后重启 Docker 并重建容器：

```json
{
  "dns": ["223.5.5.5", "1.1.1.1"]
}
```

这是宿主/守护进程级变更，会影响该机所有容器；改前先确认现有配置。

处理顺序：

1. 容器内验证：
   ```bash
   docker compose -f docker-compose.qingmeng.yml exec app getent hosts chatgpt.com
   docker compose -f docker-compose.qingmeng.yml exec app \
     curl -sS -o /dev/null -w '%{http_code}\n' https://chatgpt.com
   ```
   `getent` 无输出即 DNS 失败；`curl` 返回 401/403 也说明 TLS 已通。
2. 宿主机修复：
   ```bash
   cat /etc/resolv.conf
   getent hosts chatgpt.com
   systemctl status systemd-resolved 2>/dev/null
   ```
   如果 systemd-resolved 的 stub 文件缺失、`/etc/resolv.conf` 是坏软链，可改用静态
   `resolv.conf`（确认该机没有其他依赖后再停用 systemd-resolved）：
   ```bash
   sudo rm -f /etc/resolv.conf
   sudo tee /etc/resolv.conf >/dev/null <<'EOF'
   nameserver 223.5.5.5
   nameserver 1.1.1.1
   nameserver 8.8.8.8
   options timeout:2 attempts:3 single-request-reopen
   EOF
   ```
3. 宿主机修好后，容器的 `/etc/resolv.conf` 是创建时生成的，必须重建容器：
   ```bash
   docker compose -f docker-compose.qingmeng.yml up -d --force-recreate
   ```
4. 构建阶段 DNS 异常，使用 host 网络：
   ```bash
   docker build --network host -t qingmeng/qingmeng-gpt:1.2.4 .
   ```
   或给 compose 的 build args 配镜像源 `NPM_REGISTRY` / `PIP_INDEX_URL`。
5. 页面「设置 → 智能调度与维护 → 上游连通性」或
   `GET /api/system/upstream-diagnostics` 可分别查看 DNS、直连 TCP/TLS、默认代理三条链路。

如果只是代理节点 DNS 失败，宿主/容器 DNS 可能正常，按 8.4 查代理。

### 3.9 离线部署

本机构建并导出：

```bash
docker build --network host -t qingmeng/qingmeng-gpt:1.2.4 .
docker save qingmeng/qingmeng-gpt:1.2.4 | gzip -1 > qingmeng-1.2.4.tar.gz
```

服务器加载：

```bash
scp qingmeng-1.2.4.tar.gz docker-compose.qingmeng.yml <user>@<host>:/opt/qingmeng/
ssh <user>@<host>
cd /opt/qingmeng
gunzip -c qingmeng-1.2.4.tar.gz | docker load
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

也可以使用 `deploy/deploy-qingmeng.sh`（本地构建 → 上传 → 启动 → 健康检查）：

```bash
./deploy/deploy-qingmeng.sh root@<host> --tag 1.2.4 --port 3100
```

脚本默认端口是 3000，建议显式加 `--port 3100`。脚本会在本地存在 `config.json` 时也上传一份，
但精简 Compose 不挂载它，认证仍以服务器 `.env` 的 `QINGMENG_AUTH_KEY` 为准。

## 4. 旧数据迁入：网页上传导入

### 4.1 为什么精简 Compose 不再挂载旧库

旧方案在容器启动前自动扫描 `/legacy`、`/legacy-data`、`/old-data`、`/app/data`、`/data/legacy`
并写目标库。它适合一次性迁移，但把旧目录挂载、启动写库和升级耦合在一起，排障面更大。

精简 Compose 改为：

- 不挂载 `config.json`；
- 不挂载 legacy 目录；
- `QINGMENG_AUTO_MIGRATE=0`，入口不做自动迁移；
- 旧库通过网页上传到 `data/imports/`，先预览、再选择导入。

这样迁移失败不会影响启动，导入也可以按账号/设置/账号组/日志分开选择。

### 4.2 准备一份一致的旧库

网页上传的是一份 SQLite 文件，上传前必须保证快照一致。最稳妥的是停旧实例，再用 SQLite
备份 API 生成单文件快照（包含 WAL 中已提交的数据）：

```bash
# 旧服务器上
docker stop <旧容器名>
sqlite3 <旧数据目录>/chatgpt2api.db ".backup '/tmp/chatgpt2api.db'"
```

旧实例不便停时，也可以直接对运行中的库执行同一条 `.backup`；SQLite 备份 API 会取一致
快照。不要只复制主文件（会漏掉 WAL 中已提交的数据），也不要把 `-wal`/`-shm` 直接上传
（网页只接受单个文件）。

```bash
# 旧实例仍在运行时
sqlite3 <旧数据目录>/chatgpt2api.db ".backup '/tmp/chatgpt2api.db'"
```

复制后检查：

```bash
sqlite3 /tmp/chatgpt2api.db 'pragma integrity_check; select count(*) from accounts;'
```

至少应返回 `ok` 和账号数。旧库文件建议改名为 `chatgpt2api.db` 再上传，便于识别。如果宿主机
没有 `sqlite3`，可在任意有 SQLite 的机器上执行，或先用旧实例自带的 Python 做
`sqlite3.Connection.backup`。

### 4.3 网页操作步骤

1. 用管理员密钥登录新实例。
2. 进入「设置 → 智能调度与维护」。
3. 找到「旧项目目录同步」卡片，下方是「从数据库文件导入」。
4. 点「选择数据库文件」，选择 `.db` / `.sqlite` / `.sqlite3` 文件，再点「上传并解析」。
5. 上传完成后，文件列表会显示上传时间、大小、账号 / 设置 / 账号组 / 日志数量。
   点「预览/导入」。
6. 在账号预览表中按邮箱 / 账号 ID 搜索、翻页，勾选需要的账号；也可以「全选本页」。
7. 在「导入内容」中选择：
   - 账号：全部 / 仅已选；
   - 设置：只补缺（推荐）/ 覆盖当前；
   - 账号组：可选；
   - 调用日志：可选，界面最多 2000 条。
8. 先点「预检（不写入）」，核对新增/跳过数量。
9. 再点「确认导入」，第二次点击才会写库。
10. 完成后页面显示新增账号、跳过、设置项、账号组、日志数量；操作审计会记录
    `legacy_sync.upload` 与 `legacy_sync.import`。

导入文件保存在 `data/imports/<uuid>.db`，不会自动删除。确认无误后在文件列表点「删除」，
避免长期占用磁盘。

### 4.4 导入范围与限制

| 项目 | 网页上传导入 | 说明 |
| --- | --- | --- |
| 账号 | 支持 | 按 `access_token` 去重；已存在的账号会跳过 |
| 非保护类系统设置 | 支持 | 「只补缺」不覆盖当前值；「覆盖」会写入旧值 |
| 账号组 | 支持 | 按组 ID 去重 |
| 调用日志 | 可选 | 界面最多 2000 条；接口上限 20000 |
| 用户密钥（auth_keys） | 不支持 | 在新实例的「用户密钥」中重建 |
| 代理配置 / 代理组 / 默认出口 | 不支持 | 属于保护键，需在新实例重新配置 |
| 日志保留期 | 不支持覆盖 | 属于保护键；导入前先在当前实例调高保留期 |
| 图片 / 缩略图 / 文件任务 | 不支持 | 网页只上传 SQLite；需要时按 4.5 复制目录 |
| `config.json` | 不需要 | 管理员密钥走 `.env` |

重要限制：

- **单文件上限 2 GB**。服务端会校验 SQLite 文件头和 `accounts` 表；缺表会返回
  `不是有效的数据文件：缺少 accounts 表`。扩展名只是界面过滤，最终看文件内容。
- 反向代理的 `client_max_body_size` 必须大于上传文件；Nginx 默认会先返回 413。
- 导入日志前，先把当前实例「设置 → 基础配置 → 日志自动清理」调到足够大（例如 8760 小时）。
  否则导入后按当前保留期清理，历史日志会消失；`log_retention_hours` 不会被旧库覆盖。
- 网页导入不会自动测活。导入后在账号页选中账号，点「检测」走异步队列；或等待后台复查。
- 「覆盖」模式会用旧值覆盖当前设置，风险高于「只补缺」；正式导入前先用「预检」。
- 上传文件含账号凭据，导入完成后及时删除；不要放进代码仓库或公开备份。
- 旧库上传接口在鉴权前解析 multipart 请求体（安全审计 H-03）。不要把控制台/上传端口
  暴露给不可信网络；在反向代理/WAF 层限制请求体大小和来源，试运营前按安全审计处置。

### 4.5 超过 2 GB、需要代理和图片文件时

网页上传只适合「数据库 + 选择性账号/设置」。以下情况改用一次性迁移脚本：

- 旧库超过 2 GB；
- 需要同时复制 `images/`、`image_thumbnails/`、`files/`、`image_tasks.json` 等文件；
- SQLite 目标，且希望整库保留代理配置、账号组、调用日志等；
- 目标库是 PostgreSQL（注意：当前脚本对 PostgreSQL 只写账号与授权密钥，见下）。

先预演（不写任何东西）：

```bash
docker run --rm \
  -e QINGMENG_AUTO_MIGRATE=0 \
  -v <旧数据目录>:/legacy:ro \
  -v <新数据目录>:/data \
  qingmeng/qingmeng-gpt:1.2.4 \
  uv run python /opt/qingmeng/scripts/migrate_legacy_data.py \
    --source /legacy --target /data --dry-run
```

正式迁移。**SQLite 目标**必须为空，或显式加 `--force`；脚本会把已有目标库备份为
`.bak-时间戳`：

```bash
# 停新实例，避免并发写库
docker compose -f docker-compose.qingmeng.yml stop app

docker run --rm \
  -e QINGMENG_AUTO_MIGRATE=0 \
  -v <旧数据目录>:/legacy:ro \
  -v <新数据目录>:/data \
  qingmeng/qingmeng-gpt:1.2.4 \
  uv run python /opt/qingmeng/scripts/migrate_legacy_data.py \
    --source /legacy --target /data

# 启动级校验
docker run --rm \
  -e QINGMENG_AUTO_MIGRATE=0 \
  -v <新数据目录>:/data \
  qingmeng/qingmeng-gpt:1.2.4 \
  uv run python /opt/qingmeng/scripts/migrate_legacy_data.py \
    --target /data --verify

# 修正属主后启动
sudo chown -R 10001:10001 <新数据目录>
docker compose -f docker-compose.qingmeng.yml up -d
```

**PostgreSQL 目标**使用 `--database-url`，会直接写入目标库，不执行 SQLite 的
`.bak` 备份；迁移前先自行 `pg_dump`：

```bash
docker run --rm \
  -e QINGMENG_AUTO_MIGRATE=0 \
  -v <旧数据目录>:/legacy:ro \
  -v <新数据目录>:/data \
  qingmeng/qingmeng-gpt:1.2.4 \
  uv run python /opt/qingmeng/scripts/migrate_legacy_data.py \
    --source /legacy --target /data \
    --database-url 'postgresql://qingmeng:密码@宿主IP:5432/qingmeng'
```

PostgreSQL 注意事项：当前脚本的 `--database-url` 路径**在数据库里**只写账号与授权密钥；
系统设置、代理组/默认出口、账号组、调用日志不会自动迁入（数据文件仍会复制）。需要在
网页/代理页重建，或先在 SQLite 完成全量快照，再评估单独的 PostgreSQL 迁移方案。SQLite
目标会保留整库表（含 `system_settings`、`proxy_configuration`、账号组和日志），并复制上述文件。

SQLite 目标会默认把日志保留期写成 8760 小时，避免首次启动按旧保留期清理历史；确认后
在设置页改回实际周期。PostgreSQL 迁移没有这个保护，迁移后先把「日志保留小时」调大，
再启动或触发保留清理。

如果坚持使用自动迁移，可临时设置 `QINGMENG_AUTO_MIGRATE=1` 并只读挂载旧目录到
`/legacy`；迁移完成后移除挂载并恢复 `0`。这不是精简 Compose 的默认路径，不建议长期保留。

### 4.6 导入后的验证与回滚

验证：

1. 概览中心账号总数与旧库一致（允许已存在账号跳过）；
2. 账号页能加载、分页、搜索，抽 10 个账号做「检测」；
3. 设置页的关键项（base_url、并发、图片超时、日志保留）与预期一致；
4. 账号组已导入；网页上传路径下代理组/默认出口需手工重建，用一次性脚本的 SQLite 目标
   则代理配置随整库保留；
5. 抽一条文本/图片请求，确认 `egress_label` 和错误码正常；
6. 操作审计能看到导入记录。

回滚：

- 导入前先做一次本地备份或 R2 备份，并记录文件名；
- 导入错误设置时，停容器 → 用导入前备份还原 `data/chatgpt2api.db` → 启动；
- 只导入了账号/日志时，可在账号页按条件删除误导入账号；大批量误导入优先还原数据库；
- 不要删除 `data/imports` 中的原始文件，直到验证完成。

## 5. 升级与回滚

### 5.1 升级前检查

1. 记录当前版本与镜像 ID：
   ```bash
   curl -fsS http://127.0.0.1:3100/version
   docker inspect qingmeng-gpt --format '{{.Config.Image}} {{.Image}}'
   ```
2. 阅读 `CHANGELOG.md` 对应版本，确认是否有数据库/配置变更说明。
3. 页面「设置 → 智能调度与维护」执行一次「健康检查」；数据量大时先「一键修复」。
4. 做一次应用内备份（本地或 R2），并确认备份文件可读。
5. 确认磁盘剩余空间至少能容纳一份数据目录。

### 5.2 升级前备份

应用内「本地自动备份 → 立即备份」会生成一致的 SQLite 快照。需要覆盖图片/文件时，
停容器后打包整个数据目录：

```bash
cd /opt/qingmeng
docker compose -f docker-compose.qingmeng.yml stop app
tar -czf "data-before-upgrade-$(date +%F-%H%M).tar.gz" data
docker compose -f docker-compose.qingmeng.yml start app
```

PostgreSQL 用 `pg_dump` 或 R2 备份；本地自动备份会跳过。

### 5.3 执行升级

方式 A：从镜像仓库拉取固定 tag：

```bash
cd /opt/qingmeng
# 先把 .env 的 QINGMENG_IMAGE 改为目标版本
docker compose -f docker-compose.qingmeng.yml pull app
docker compose -f docker-compose.qingmeng.yml config --quiet
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

方式 B：离线镜像：

```bash
docker load -i qingmeng-1.2.4.tar.gz
# .env 的 QINGMENG_IMAGE 与 load 的 tag 一致
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

方式 C：本地重新构建（仅开发/无仓库环境）：

```bash
docker compose -f docker-compose.qingmeng.yml build --pull
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

不要使用 `docker compose down -v`；数据在宿主 `./data`，但 `-v` 会删除匿名/命名卷，
属于不必要的风险操作。

### 5.4 升级后验证

1. `docker compose ps` 为 `healthy`，没有反复 `restarting`；
2. `GET /version` 返回目标版本；
3. 登录控制台，概览中心 `status=ok`、`healthy=true`，账号总数与升级前一致；
4. 设置 → 智能调度与维护：硬件档位、调度模式、队列、代理健康正常；
5. 账号页抽 5–10 个账号检测；发一条文本请求和一条图片请求；
6. 日志无 traceback、无 `unable to open database file`、无数据库 schema 错误；
7. 本地备份/R2 备份仍能执行；
8. 观察 30 分钟：错误率、队列积压、CPU/内存、`scale_percent`。

### 5.5 回滚

镜像回滚：

```bash
cd /opt/qingmeng
# 编辑 .env，把 QINGMENG_IMAGE 改回上一个版本，例如 1.1.7.3
docker compose -f docker-compose.qingmeng.yml config --quiet
docker compose -f docker-compose.qingmeng.yml up -d --no-build
curl -fsS http://127.0.0.1:3100/version
```

如果新版本已经改了数据库结构，旧镜像可能读不了新库。此时不要只回滚镜像，按数据回滚：

```bash
cd /opt/qingmeng
docker compose -f docker-compose.qingmeng.yml stop app
mv data "data.failed-$(date +%F-%H%M)"
tar -xzf data-before-upgrade-YYYYMMDD-HHMM.tar.gz -C /opt/qingmeng
chown -R 10001:10001 data
# 确认 .env 的 QINGMENG_IMAGE 是旧版本
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

PostgreSQL 则恢复升级前的 `pg_dump`，再启动旧镜像。

回滚验证与升级验证相同：`/version`、控制台登录、账号数、调度、队列、备份、抽一条请求。
回滚后至少保留旧镜像和升级前备份 7 天；确认稳定前不要删除 `data.failed-*` 现场。

### 5.6 版本与 tag 约定

- 生产固定 `qingmeng/qingmeng-gpt:X.Y.Z`，保留上一个稳定 tag 的本地镜像；
- 需要更强不可变性时使用 digest：`qingmeng/qingmeng-gpt:1.2.4@sha256:<digest>`；
- `latest` 只用于测试；
- 每次升级记录：时间、旧/新 tag、镜像 ID、备份文件、验证结果、回滚决定。

## 6. 备份与恢复

### 6.1 本地自动备份（SQLite）

位置：`data/backups/chatgpt2api-YYYYMMDD-HHMMSS.db`；配置：`data/local_backup.json`。
默认关闭、间隔 24 小时、保留 7 份。开启后后台循环每 10 分钟检查一次是否到期，到期用
SQLite 备份 API 生成一致快照。

操作步骤：

1. 设置 → 智能调度与维护 → 本地自动备份；
2. 勾选启用，设置间隔（建议 24 小时）和保留份数（建议 7–14）；
3. 保存后点「立即备份」；
4. 确认列表出现文件、大小合理；
5. 抽一份做完整性检查：

```bash
sqlite3 data/backups/chatgpt2api-YYYYMMDD-HHMMSS.db \
  'pragma integrity_check; select count(*) from accounts;'
```

限制：只备份 Application Database，不含 `images/`、`files/`、`image_tasks.json`；PostgreSQL
下会跳过。需要完整备份时用 R2 或停容器打包。

### 6.2 R2 备份

设置 → R2 备份：

- 配置 Account ID、Access Key、Secret、Bucket、前缀；
- 「测试连接」通过后，选择备份内容：Application Database 始终包含；图片任务、可编辑文件、
  图片目录按需勾选（图片目录默认不包含）；
- 可启用加密，必须保存好口令；丢失后不可恢复；
- 设置间隔（默认 360 分钟）和保留份数（默认 10）；
- 点「立即备份」，确认对象列表出现 `backup-*.tar.gz`（或 `.enc`）。

R2 备份包结构：`backup-metadata.json`、`data/application-database.sqlite3`（或
`data/application-database.pgdump`），以及按需包含的 `data/images`、`data/files` 等。

### 6.3 从备份恢复

**SQLite**

1. 停容器；
2. 把当前 `data` 移开或备份；
3. 解包备份，取 `data/application-database.sqlite3`；
4. 覆盖 `data/chatgpt2api.db`，删除旧的 `-wal` / `-shm`；
5. `chown 10001:10001 data/chatgpt2api.db`，必要时 `chmod 640`；
6. 启动，验证账号数、登录、抽一条请求。

```bash
cd /opt/qingmeng
docker compose -f docker-compose.qingmeng.yml stop app
mv data "data.before-restore-$(date +%F-%H%M)"
mkdir -p data
mkdir -p /tmp/qingmeng-restore
tar -xzf backup.tar.gz -C /tmp/qingmeng-restore
cp /tmp/qingmeng-restore/data/application-database.sqlite3 data/chatgpt2api.db
chown 10001:10001 data/chatgpt2api.db
chmod 640 data/chatgpt2api.db
docker compose -f docker-compose.qingmeng.yml up -d --no-build
```

如果备份包含 `data/images`、`data/files` 等目录，把它们一并放回 `data/` 并
`chown -R 10001:10001 data`；只还原数据库不会恢复图片文件。

如果备份是加密的 `.tar.gz.enc`，先解密（口令只在本机输入）：

```bash
read -rsp 'backup passphrase: ' BACKUP_PASSPHRASE; echo
export BACKUP_PASSPHRASE
openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 \
  -pass env:BACKUP_PASSPHRASE \
  -in backup-YYYYMMDDTHHMMSSZ-xxxx.tar.gz.enc \
  -out backup.tar.gz
unset BACKUP_PASSPHRASE
```

**PostgreSQL**

停应用，使用能访问目标库的 `pg_restore`：

```bash
pg_restore --clean --if-exists --no-owner --no-privileges \
  -d "$DATABASE_URL" data/application-database.pgdump
```

恢复后启动并验证。不要在应用运行时覆盖 PG 库。

### 6.4 恢复演练

试运营期至少做一次完整演练：在非生产目录恢复一份备份，启动临时端口实例，确认能登录、
账号数一致、抽一条请求成功，再销毁临时实例。只看到“备份成功”不算验证。

## 7. 首次启动 / 试运营检查

完整清单见 [runbook.md](runbook.md)。最少确认：

| 类别 | 通过标准 |
| --- | --- |
| 健康检查 | `docker compose ps` 为 healthy；`/version` 返回 1.2.4；`/api/dashboard` 的 `healthy=true` |
| 权限 | 应用 PID 的 UID 为 10001；以 10001 身份写 `/data` 成功 |
| 调度 | 硬件档位识别正确；模式为「智能自动」；`scale_percent=100`；队列无积压 |
| 调试日志 | 默认关闭；需要时按体积/时间限额开启，问题复现后导出并关闭 |
| 备份 | 本地备份已开启并成功生成；R2 已测试连接；至少一份备份可读 |
| 告警 | 低可用告警已配置 Webhook；「立即体检」能返回报告；`/metrics` 可抓取 |
| 并发上限 | `account_processing_concurrency`、`image_account_concurrency` 已按容量设置；准入上限符合预期 |
| 代理 | 代理节点健康；没有大面积 `direct (proxy group degraded)` |
| 业务冒烟 | 登录、账号检测、文本、图片、搜索各成功一次 |

## 8. 常见故障

以下命令中的 `$AUTH_KEY` 指 `.env` 里的管理员密钥，`$BASE_URL` 指实际访问地址
（本机默认 `http://127.0.0.1:3100`）。不要把密钥写进工单或截图。

### 8.1 `unable to open database file`

这是 SQLite 打不开数据库文件时的报错，优先查 **目录写权限**，不是先怀疑数据库损坏。

排查：

```bash
cd /opt/qingmeng

# 入口日志：是否修正过权限、是否 chown 失败
docker compose -f docker-compose.qingmeng.yml logs app | \
  grep -E '修正数据目录权限|chown 失败|不可写|启动：DATA_DIR'

# 应用真实 UID
docker compose -f docker-compose.qingmeng.yml exec app sh -c 'grep ^Uid /proc/1/status'

# 10001 写测试
docker compose -f docker-compose.qingmeng.yml exec -u 10001 app sh -c \
  'touch /data/.write-test && rm /data/.write-test && echo WRITABLE'

# 宿主属主；db、wal、shm 都要看
stat -c '%u:%g %a %n' data data/chatgpt2api.db data/chatgpt2api.db-wal 2>/dev/null

# 路径是否被误建为目录
ls -ld data data/chatgpt2api.db
```

处理：

1. 宿主属主不是 10001：停容器 → `chown -R 10001:10001 data` →
   `chmod -R u+rwX,g+rwX data` → 启动；
2. 只有 db/wal 深层文件是 root：同样整树 `chown -R`；入口只检查顶层，不要依赖它修深层；
3. `data` 是只读挂载/网络盘：改为本地可写盘；SQLite 不要放 NFS/CIFS；
4. 磁盘/inode 满：清理 `backups`、`imports`、旧图片/日志或扩容；
5. `chatgpt2api.db` 是目录或文件类型错误：先备份现场，再把正确文件放回；
6. 设置了 `DATABASE_URL` 时，确认指向可写路径；SQLite 绝对路径形如
   `sqlite:////data/chatgpt2api.db`（四个斜杠），三个斜杠是相对路径；
7. 数据库损坏：用 `pragma integrity_check` 检查，按第 6 节还原备份，不要直接删除主库；
8. PostgreSQL 不会报这个 SQLite 错误；若连接失败，查 `DATABASE_URL`、`pg_isready` 和网络。

修复后以 `/version`、控制台登录和一次账号检测确认。

### 8.2 `config.json` 变成目录

旧版 `docker-compose.yml` 有：

```yaml
volumes:
  - ./config.json:/opt/qingmeng/config.json:ro
```

当宿主机不存在 `config.json` 文件时，Docker 会把绑定挂载的源创建成 **目录**。应用读取时
会打印类似警告并忽略：

```text
Warning: config.json at '/opt/qingmeng/config.json' is a directory; ignoring it.
```

当前版本认证优先读 `QINGMENG_AUTH_KEY`，所以目录本身通常不会直接导致启动失败；但如果没有
环境变量，应用侧会报 `auth-key 未设置`（使用 Compose 时通常先在变量插值阶段被 `:?` 拦住）。
精简 Compose 不挂载 `config.json`，不会复发。

处理：

```bash
cd /opt/qingmeng
docker compose -f docker-compose.yml down
ls -ld config.json
```

如果 `config.json` 是真实文件并保存了 `auth-key`，先在本机安全读取并写入 `.env`，再移走；
不要把密钥贴到工单或聊天记录：

```bash
python3 -c 'import json; print(json.load(open("config.json"))["auth-key"])'
```

然后二选一：

```bash
# 二选一：
# 空目录（Docker 自动创建的典型情况）
rmdir config.json

# 非空目录或真实文件：先移走备份，不要直接 rm -rf
# mv config.json "config.json.bak-$(date +%F-%H%M)"
```

确认 `.env` 中有非空 `QINGMENG_AUTH_KEY`，然后切到精简 Compose：

```bash
grep -c '^QINGMENG_AUTH_KEY=.' .env
chmod 600 .env
docker compose -f docker-compose.qingmeng.yml up -d
docker compose -f docker-compose.qingmeng.yml logs app | grep -i 'config.json'
```

日志不再出现该警告、`/version` 正常即修复。如果目录非空，先备份内容再处理，不要直接删除。

### 8.3 DNS 解析失败

详见 3.8 和 runbook。快速判断：

```bash
docker compose -f docker-compose.qingmeng.yml exec app getent hosts chatgpt.com
docker compose -f docker-compose.qingmeng.yml exec app \
  curl -sS -o /dev/null -w '%{http_code}\n' https://chatgpt.com
curl -fsS -H "Authorization: Bearer $AUTH_KEY" \
  http://127.0.0.1:3100/api/system/upstream-diagnostics
```

宿主机修好 `/etc/resolv.conf` 后必须 `--force-recreate`，否则容器沿用创建时的旧配置。
构建阶段失败用 `docker build --network host`。只影响代理节点时按 8.4 处理。

### 8.4 请求走 `direct` 或 `direct (proxy group degraded)`

`egress_label` 的两种含义：

| 标签 | 含义 |
| --- | --- |
| `direct` | 没有配置代理，或账号/全局出口明确指定直连；属于正常配置 |
| `direct (proxy group degraded)` | 代理组配置了节点，但当前没有可用节点（全部熔断/冷却），系统回退直连 |

如果上游对直连出口返回 403/Cloudflare 拦截，请求会失败；此时直连不是“正常回退”，需要修代理。

排查：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" \
  http://127.0.0.1:3100/api/system/proxy-health
curl -fsS -X POST -H "Authorization: Bearer $AUTH_KEY" \
  http://127.0.0.1:3100/api/system/proxy-health/probe
curl -fsS -H "Authorization: Bearer $AUTH_KEY" \
  http://127.0.0.1:3100/api/system/upstream-diagnostics
```

页面：「设置 → 智能调度与维护 → 代理健康与自动切换」。

处理：

1. 确认代理组有节点、节点启用、URL/账号密码正确；
2. 看每个节点的 `state` 与 `cooldown_remaining`；全部熔断时先修节点或等冷却；
3. 检查账号是否绑定了另一个代理组，或全局默认出口被设为 `direct`；
4. 在服务器上直测节点（把 `$PROXY_URL` 换成实际节点 URL）：
   ```bash
   curl -x "$PROXY_URL" -I https://chatgpt.com --max-time 15
   ```
5. 修好后「立即探测」，再发一条请求，确认 `egress_label` 变为 `proxy:...`；
6. 如果只有图片请求走直连，检查节点「图片并发」配额是否被占满；配额为 0 表示不限。

### 8.5 端口映射 / 外网访问失败

精简 Compose 的映射是 `${QINGMENG_PORT:-3100}:80`；容器内是 80，不是 3100。

```bash
cd /opt/qingmeng
docker compose -f docker-compose.qingmeng.yml ps
docker compose -f docker-compose.qingmeng.yml port app 80
ss -lntp | grep ':3100' || true
curl -v http://127.0.0.1:3100/version
curl -v http://<公网IP>:3100/version
```

判断：

| 现象 | 处理 |
| --- | --- |
| `ps` 没有端口映射 | 检查 compose 的 `ports`；确认启动的是正确文件 |
| 本机 `curl` 通、外网不通 | 宝塔「安全」、云安全组、宿主 `ufw`/`firewalld` 放行 |
| `port is already allocated` | `ss -lntp` 找占用进程；改 `QINGMENG_PORT` 或停冲突服务 |
| 改了 `QINGMENG_PORT` 但端口没变 | `up -d --force-recreate` 重建容器 |
| 域名访问 502 | 反代目标应是 `127.0.0.1:3100`；先确认本机 `curl` 通 |
| 上传旧库 413 | 反代 `client_max_body_size` 太小；按 3.6 放大 |
| 长请求/图片 504 | 反代 `proxy_read_timeout` 太短；关闭 `proxy_buffering` |
| 页面 404 但 `/version` 正常 | 镜像缺少前端产物 `web_dist/index.html`；重新构建/使用完整镜像 |

端口速查：精简 Compose 与 `.env.qingmeng.example` 是 3100；默认 `docker-compose.yml` 是 3000；
`deploy-qingmeng.sh` 默认 3000。不要凭旧文档猜端口，以 `docker compose port app 80` 为准。

### 8.6 其他常见问题

| 现象 | 排查 |
| --- | --- |
| 启动前 Compose 报 `required variable QINGMENG_AUTH_KEY is missing a value` | `.env` 中密钥为空；填 32 位以上随机串，`chmod 600` |
| 登录 401 | 密钥与 `.env` 不一致；改完需 `up -d` 重建容器 |
| 容器反复重启 | 看入口日志的权限、密钥、DNS、数据库错误；健康检查失败不会自动重启，退出才会 |
| 队列积压 | 看 `queued_tokens`、`workers/worker_target`、失败率；检查上游/代理；可取消任务 |
| 调度降配后不恢复 | 看 `restore_in_seconds`、CPU/内存阈值、是否手动锁定；负载均值只作诊断 |
| 检测很慢 | 上游限速/代理延迟；看队列 `rate_per_second` 和失败率 |
| 数据库锁 | SQLite 单写者；降低并发/维护窗口；>5 万账号考虑 PostgreSQL |
| 代理全部熔断 | 「立即探测」；检查节点 URL/认证/DNS；全部不可用会回退直连 |
| 磁盘增长快 | 清 `data/imports`、`data/backups`、旧图片/调试日志；检查保留期 |
| R2 备份失败 | 凭据/Bucket/网络；加密时检查 `openssl` 和口令；看设置页最近错误 |
| 旧库上传后账号少 | 旧库快照不含 WAL；停旧实例后重新复制；或看是否已存在被跳过 |

## 9. 安全建议

- 管理员密钥使用 32 字节以上随机串，`.env` 权限 600；不要提交到仓库或截图。
- 容器入口以 root 修正数据目录后立即 `gosu` 降权，应用进程为 10001；不要额外加
  `--privileged`、不要挂载 Docker socket。
- 对外必须 HTTPS；反向代理层加访问控制、限速和上传大小限制。
- `data/imports`、`data/backups`、调试日志都含账号/调用信息；导入完成删除旧库文件，
  备份加密并限制访问。
- 调试日志行会替换 JWT/Bearer/sk- 等敏感字段，但导出包的 `system-info.json` 仍可能包含
  配置摘要/服务凭据；把整个调试包按最高敏感级别管理，不要外发。问题复现后关闭并清理。
- 试运营前处置管理员密钥强度、调试导出敏感信息、旧库上传来源限制三项安全前置；
  本手册的健康检查不替代安全验收。
- SQLite 不要放 NFS/CIFS；备份文件不要与主库放在同一块故障盘。
- 宝塔面板/ Docker 权限等同 root；限制面板账号与来源 IP。
- 旧 `config.json` 只保留认证兜底用途；精简部署不需要它，避免留下明文密钥。
