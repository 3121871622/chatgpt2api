# 试运营运维手册（Runbook）

Status: current

适用版本：v1.1.8。部署、迁移、升级的完整步骤见 [deployment.md](deployment.md)；
本手册用于试运营期的日常检查、容量观察、备份演练和故障处置。

回滚责任：当班运维负责人决定并执行回滚；数据恢复以升级/变更前备份为准。
未确认根因前保留失败现场（容器日志、`data.failed-*`、备份文件），不要先删除数据。

> 试运营安全前置：管理员密钥强度、调试导出包的敏感信息、旧库上传的来源限制，这三项
> 必须在对外开放前处置完；本手册的检查清单不替代安全验收。未完成修复或等价缓解前，
> 不要对外开放。

## 0. 范围与权威

- 适用范围：v1.1.8 精简 Compose（`docker-compose.qingmeng.yml`）、宝塔面板、裸机；
  旧默认 Compose 只按 [deployment.md](deployment.md) 的兼容说明处理。
- 权限：Docker、数据目录 `chown`、防火墙操作通常需要宿主 root/sudo；管理接口需要
  `.env` 中的 `QINGMENG_AUTH_KEY`。
- 数据风险：导入、恢复、回滚都会改 `data/`；先备份，保留现场，确认后再删除。
- 持久化：容器内 `/data`，宿主对应 `./data`；本地备份、导入文件、调试日志都在这里。
- 权威顺序：部署步骤以 [deployment.md](deployment.md) 为准；版本变化见
  [`../CHANGELOG.md`](../CHANGELOG.md)。

## 1. 使用方式

以下命令默认在部署目录执行。路径是示例，按实际部署替换：

```bash
cd /opt/qingmeng
COMPOSE_FILE=docker-compose.qingmeng.yml
BASE_URL=http://127.0.0.1:3100
export COMPOSE_FILE BASE_URL

# 管理员密钥从 .env 读取，只放在当前 shell，不要写进脚本或工单
export AUTH_KEY='<QINGMENG_AUTH_KEY>'
```

约定：

- `app` 是 Compose 服务名，容器名是 `qingmeng-gpt`；
- 管理接口需要 `Authorization: Bearer $AUTH_KEY`；`/version` 不需要；
- 先只读检查，再变更；任何数据变更前先备份；
- 命令输出里不要粘贴完整密钥、账号 token 或备份口令。

## 2. 试运营检查清单

### 2.1 上线前（T-1）

- [ ] 记录目标版本与镜像：`QINGMENG_IMAGE=qingmeng/qingmeng-gpt:1.1.8`，保存镜像 ID/digest
- [ ] `.env` 中 `QINGMENG_AUTH_KEY` 为 32 字节以上随机串，权限 600
- [ ] 已处置管理员密钥强度、调试导出敏感信息、旧库上传来源限制三项安全前置；
      调试导出包不外发，旧库上传只对可信来源开放
- [ ] `data/` 位于本地 Linux 数据盘，剩余空间 ≥ 当前数据体积的 2 倍
- [ ] 宿主端口（默认 3100）已在宝塔「安全」、云安全组、宿主防火墙放行
- [ ] HTTPS/反向代理已配置，`QINGMENG_BASE_URL` 为最终对外地址
- [ ] 本地自动备份已开启并手动成功一次；R2 备份已「测试连接」并成功一次
- [ ] 低可用告警已配置 Webhook，「立即体检」能返回报告
- [ ] 调试日志策略已确定：默认关闭，或设置时间/体积上限
- [ ] `account_processing_concurrency`、`image_account_concurrency` 已按容量设置
- [ ] 调度模式为「智能自动」，阈值符合机器规格
- [ ] 代理组至少两个健康节点；没有代理时确认直连出口可用
- [ ] 升级前镜像 tag、备份文件、回滚步骤已写入交接记录

### 2.2 启动后 30 分钟

- [ ] `docker compose ps` 为 `healthy`，`RestartCount` 没有持续增长
- [ ] 首次日志包含 DNS 正常、权限修正（或跳过）、`版本=1.1.8`，没有 traceback
- [ ] `/version` 返回 1.1.8；控制台可登录
- [ ] `/api/dashboard` 的 `status=ok`、`healthy=true`、`metrics.ready=true`
- [ ] 应用 PID 的 UID 为 10001；以 10001 身份写 `/data` 成功
- [ ] 调度：硬件档位正确、模式智能自动、`scale_percent=100`
- [ ] 队列：`queued_tokens` 接近 0，`failed_total` 没有持续增长
- [ ] 代理健康：没有大面积 `down`，`fallback_reference` 符合预期
- [ ] 导入/已有账号抽 10 个做「检测」，进度可走完
- [ ] 文本、图片、搜索各成功一次；图片链接可公网访问
- [ ] 调试日志处于关闭或已配置限额状态
- [ ] 备份：手动执行一次成功，文件大小合理
- [ ] 告警：手动体检成功，Webhook 能收到测试消息
- [ ] 观察 30 分钟：CPU/内存、错误率、队列、`scale_percent` 稳定

### 2.3 每日

- [ ] `docker compose ps` 与 `RestartCount`
- [ ] `/version`、`/api/dashboard` 的 `healthy` / `metrics.ready`
- [ ] 调度 `scale_percent`、`adjustments`、`restore_in_seconds`
- [ ] 队列 `queued_tokens`、`failed_total`、最老任务等待时间
- [ ] 代理健康计数与 `direct (proxy group degraded)` 是否异常增多
- [ ] 磁盘使用率、`data/imports`、`data/backups`、调试日志体积
- [ ] 最近一次本地/R2 备份状态；失败则当天处理
- [ ] 告警最近报告；低可用/低续期是否触发
- [ ] 错误日志扫描：traceback、database、permission、DNS、429/503

### 2.4 每周

- [ ] 低峰期执行一次数据库「健康检查」，碎片率 ≥ 20% 时「一键修复」
- [ ] 抽一份备份做恢复演练（可每月一次，至少每季度一次）
- [ ] 检查证书/域名/反代超时与上传大小限制
- [ ] 清理无用的 `data/imports`、旧调试日志、过期本地备份
- [ ] 复核容量：账号数、队列峰值、CPU/内存、数据库体积
- [ ] 复核告警阈值与值班联系人

## 3. 健康检查

### 3.1 容器与进程

```bash
docker compose -f "$COMPOSE_FILE" ps
docker inspect -f \
  '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}' \
  qingmeng-gpt

docker compose -f "$COMPOSE_FILE" logs --since=30m app | \
  grep -Ei 'traceback|exception|error|failed|permission|sqlite|dns' || true
```

期望：

- `State.Status=running`，`Health.Status=healthy`，`RestartCount` 不增长；
- 日志没有反复重启、数据库打不开、权限失败、DNS 失败；
- 健康检查失败只会标记 `unhealthy`，不会自动重启容器；只有进程退出才触发
  `restart: unless-stopped`。

### 3.2 数据目录写权限

```bash
docker compose -f "$COMPOSE_FILE" exec app sh -c 'grep ^Uid /proc/1/status'
docker compose -f "$COMPOSE_FILE" exec -u 10001 app sh -c \
  'touch /data/.write-test && rm /data/.write-test && echo WRITABLE'
```

`grep ^Uid` 应返回四个 10001；`WRITABLE` 表示应用用户可以写数据目录。
`docker compose exec app id` 可能显示 root，因为镜像没有设置 `USER`，入口先 root 再
`gosu`；不要用 `id` 判断应用进程身份。

### 3.3 HTTP 探针

```bash
curl -fsS "$BASE_URL/version"

curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/dashboard" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("status",d["status"],"healthy",d["healthy"],"version",d["version"]); print("metrics",d["metrics"]["ready"],d["metrics"]["stale"],d["metrics"]["failure_reason"]); print("accounts",{k:d["accounts"][k] for k in ("total","active","limited","abnormal","disabled")}); print("uptime",d["runtime"]["service_uptime_seconds"])'

curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/load" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("admission",d["admission"]); print("thread_tokens",d["thread_tokens"]); print("queue",d["queue"])'
```

判读：

| 字段 | 正常 | 异常时先看 |
| --- | --- | --- |
| `status` / `healthy` | `ok` / `true` | `metrics.failure_reason`、账号池、数据库 |
| `metrics.ready` / `stale` | `true` / `false` | 统计投影刷新、启动日志 |
| `accounts.total` | 与预期一致 | 导入、自动清理、数据库 |
| `admission.inflight/queued` | 在途低于上限，排队接近 0 | 并发、上游延迟、线程池 |
| `queue.queued_tokens` | 接近 0 | 队列、代理、上游限速 |

`/metrics` 是 Prometheus 文本，需管理员凭证：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/metrics" | \
  grep -E '^qingmeng_(pool_accounts|pool_refresh_ratio_percent|queue_active_jobs|queue_queued_tokens|inflight_requests|queued_requests|request_limit|requests_rejected_total|database_size_bytes)'
```

### 3.4 健康判定

| 级别 | 条件 | 动作 |
| --- | --- | --- |
| 绿 | 容器 healthy；`/version` 正常；dashboard `healthy`；队列不积压；备份最近成功 | 保持观察 |
| 黄 | `metrics.stale`、`scale_percent<100` 但可恢复、队列短时积压、备份最近一次失败、告警阈值触发 | 30 分钟内定位；先查上游/代理/备份，不急于重启 |
| 红 | 容器反复重启/不健康；`/version` 失败；`unable to open database file`；磁盘 <5%；备份连续失败；账号池不可用 | 立即按第 8 节处理；必要时回滚，先保留现场 |

## 4. 调度、队列与并发上限

### 4.1 日常调度检查

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/scheduler" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("mode",d["mode"],"level",d["level"],"scale",d["scale_percent"]); print("hardware",d["hardware"]["effective_tier_label"],d["hardware"]["cpu_cores"],"cores",d["hardware"]["memory_total_gb"],"GB"); print("load",d["load"]["cpu_percent"],d["load"]["memory_percent"]); print("adjustments",d["adjustments"],"restore_in",d["restore_in_seconds"]); print("base",d["base"]); print("effective",d["effective"]); print("events",d["events"][-3:])'
```

页面路径：「设置 → 智能调度与维护 → 智能自适应负载调度」。

要点：

- 默认「智能自动」：CPU ≥ `cpu_high`（默认 85%）或内存 ≥ `mem_high`（默认 90%）时逐级降配；
  CPU/内存分别低于 `cpu_low=70` / `mem_low=80` 并持续 `restore_hold_seconds=300` 秒后回弹；
- 系统负载均值只作诊断，不触发降档；
- 「手动锁定」完全按保存值运行，不再自动降配/回弹；
- 调度只改运行时生效值，不覆盖你保存的并发/线程配置；
- 策略校验要求恢复阈值小于过载阈值，保存前会拒绝非法组合。

### 4.2 降配或队列积压时

1. 看 `scale_percent`、`load.cpu_percent`、`load.memory_percent`、`adjustments`、`restore_in_seconds`；
2. 看 `/api/system/queue` 的 `stats.workers/worker_target`、`queued_tokens`、`failed_total`；
3. 看上游/代理：`/api/system/proxy-health`、`/api/system/upstream-diagnostics`、错误码 429/503；
4. 如果 CPU/内存确实高：先降业务量、查慢请求/图片任务、扩机器或切 PostgreSQL；
5. 如果资源不高但持续降配：检查阈值是否被调得过窄、是否有单进程采样异常；不要直接切手动掩盖；
6. 如果队列积压但 workers 已满：查上游限速、代理延迟、账号可用性；必要时取消任务；
7. 任何阈值/并发变更一次只改一项，记录变更前后指标。

队列接口：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/queue" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["stats"]); print([(j["label"],j["state"],j["processed"],j["total"],j["failed"]) for j in d["jobs"]])'
```

### 4.3 并发上限速查

| 参数 | 默认/范围 | 作用 | 备注 |
| --- | --- | --- | --- |
| `QINGMENG_MAX_REQUEST_CONCURRENCY` | 未设置时按核数 ×16，64–4096 | 全局请求准入上限 | 32 核约 512；64 核约 1024；可用环境变量覆盖 |
| `account_processing_concurrency` | 按硬件档位种子：24/96/256/512 | 账号批量任务并发 | 网页「基础配置 → 账号批量任务并发」可改 |
| `image_account_concurrency` | 1，范围 1–3 | 单账号同时处理的图片请求 | 网页「基础配置 → 单账号图片并发」 |
| `account_check_workers` | 默认 64，范围 1–8192 | 检测/AT 刷新队列工作线程 | 首次启动按档位种子；调度器按负载缩放 |
| `image_task_workers` | 默认 16，范围 1–1024 | 出图任务线程 | 服务启动时创建固定线程池；改后需重启，调度器不缩放它 |
| `worker_threads` | 默认 128，范围 8–8192 | 历史线程预算字段 | 运行时线程池已跟随准入预算，不要拿它当真实并发上限 |

准入超时返回 **503 + Retry-After**（通常 5 秒）：普通请求排队上限 60 秒，图片请求 180 秒。
客户端应重试；不要通过无限加线程解决，先看 `admission.queued` 和上游延迟。

### 4.4 容量参考

| 账号规模 | 建议 |
| --- | --- |
| ≤ 5 万 | 单机 SQLite，8 核/16 GB 起 |
| 5 万–30 万 | PostgreSQL，16 核/32 GB 起 |
| 30 万–100 万 | PostgreSQL，32 核/128 GB 起，独立数据库盘 |
| 百万以上 | 分片/多实例 + PostgreSQL，单独压测后再放量 |

SQLite 单写者，不要靠加线程提升写入；图片/文件占磁盘，备份与清理策略要同步放大。

## 5. 调试日志

默认关闭。开启后写入 `data/debug-logs/debug-*.log`（JSON Lines），按时间或体积滚动；
单文件上限 64 MB，导出 zip 上限 300 MB，导出包含 `system-info.json`。
日志行中的 JWT、Bearer、`sk-` 等会被替换，但导出包的 `system-info.json` 仍可能包含
配置摘要/服务凭据；整个调试包按最高敏感级别管理，不要外发。

### 5.1 何时开启

- 需要复现具体请求失败、选号慢、SSE 断流、导入异常；
- 生产问题难以从 `docker compose logs` 的摘要定位；
- 开启前记录时间、账号/请求特征、预期复现步骤。

不要长期开启 `debug` 级别；正常试运营用 `info` 或关闭。

### 5.2 操作步骤

1. 「设置 → 智能调度与维护 → 调试日志」；
2. 勾选启用；
3. 选择保留方式：
   - 按时间：建议 1–3 天；
   - 按体积：建议 256–512 MB，允许上浮 64–128 MB；
4. 采集级别：常规用 `info`，定位深层问题用 `debug`；
5. 保存，复现问题；
6. 点「下载调试包」，保存到受控位置；
7. 问题结束后关闭调试日志，点「清空日志」并确认。

检查状态：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/debug-logs" \
  | python3 -m json.tool
```

关注 `active`、`file_count`、`total_bytes`、`budget_bytes`、`slack_bytes`、`directory`。
如果磁盘紧张，先降预算/清空，不要等写满。

## 6. 备份与恢复演练

### 6.1 本地自动备份（SQLite）

```bash
# 查看状态与最近报告
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/local-backup" \
  | python3 -m json.tool

# 立即备份
curl -fsS -X POST -H "Authorization: Bearer $AUTH_KEY" \
  "$BASE_URL/api/system/local-backup/run" | python3 -m json.tool
```

期望 `report.file` 非空、`size_bytes` 合理、`kept` 不超过配置份数。文件在
`data/backups/chatgpt2api-*.db`。本地备份只覆盖 SQLite Application Database，不含图片、
文件和 PostgreSQL；PostgreSQL 会跳过。

### 6.2 R2 备份

页面：「设置 → R2 备份」。步骤：

1. 填写 Account ID、Access Key、Secret、Bucket、前缀；
2. 「测试连接」通过；
3. 选择内容：Application Database 始终包含；图片任务、可编辑文件、图片目录按需；
4. 加密可选；启用后必须保存口令，丢失后不可恢复；
5. 设置间隔与保留份数；
6. 「立即备份」，确认对象列表出现 `backup-*.tar.gz` 或 `.enc`。

检查最近状态：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/backups" \
  | python3 -m json.tool
```

### 6.3 恢复演练

至少每季度一次，在非生产目录做：

1. 取一份最新备份，确认可解包；
2. 停临时实例，准备空 `data/`；
3. SQLite：把 `data/application-database.sqlite3` 还原为 `data/chatgpt2api.db`，
   删除旧 `-wal`/`-shm`，`chown 10001:10001`；
4. PostgreSQL：`pg_restore` 到临时库；
5. 启动临时实例（换端口），验证：
   - `/version` 正常；
   - 控制台能登录；
   - 账号总数与备份来源一致；
   - 抽一条文本/图片请求成功；
6. 如果备份包含 `data/images`、`data/files`，一并还原并 `chown -R 10001:10001 <数据目录>`；
7. 记录恢复耗时、备份文件、验证结果；销毁临时实例。

只看到“备份成功”不算验证；必须有一次可启动、可登录、可请求的恢复证据。

## 7. 告警

### 7.1 号池低可用告警

页面：「设置 → 智能调度与维护 → 凭据状态 → 低可用告警」。

- 可用账号低于 `min_usable`，或可续期占比低于 `min_refresh_ratio_percent` 时触发；
- 可配置 Webhook；同一问题在冷却期内只报一次；
- 「立即体检」会强制检查并推送（即使冷却中），但只有存在告警项时才发 Webhook；
- 默认阈值：可用账号 1、可续期占比 10%；按试运营规模调整。

测试 Webhook 的稳妥方式：临时把 `min_usable` 调到高于当前可用数，保存 → 立即体检 →
确认收到 → 调回原值。不要把测试阈值留到生产。

Webhook 负载是通用 JSON：

```json
{
  "event": "pool_health",
  "checked_at": "2026-10-25 00:00:00",
  "warnings": ["可用账号 0 低于阈值 1"],
  "metrics": {"total": 0, "usable": 0, "recoverable": 0, "unavailable": 0, "refresh_valid": 0, "refresh_ratio_percent": 0}
}
```

钉钉/飞书机器人通常要求特定字段，直接填其 Webhook 可能被拒绝；用中转函数转换，或接
支持通用 JSON 的告警平台。

### 7.2 外部探针

- 存活：`GET /version`，无需鉴权，适合 1 分钟一次的 uptime 探针；
- 业务健康：`GET /api/dashboard`，需管理员 Bearer，适合 5 分钟一次；
- 指标：`GET /metrics`，需管理员 Bearer，接 Prometheus；
- 日志：`docker compose logs` 扫描 traceback/数据库/权限/DNS/429/503。

百万号池的 `/metrics` 会遍历账号做分桶统计，抓取间隔从 30–60 秒起，不要配得比业务请求
还密；`/api/dashboard` 也不适合秒级轮询。

Prometheus 抓取示例（token 放文件，不要写进仓库）：

```yaml
scrape_configs:
  - job_name: qingmeng
    metrics_path: /metrics
    authorization:
      credentials_file: /etc/prometheus/qingmeng-token
    static_configs:
      - targets: ["127.0.0.1:3100"]
```

### 7.3 告警响应

| 告警 | 先查 | 处理 |
| --- | --- | --- |
| 可用账号低于阈值 | 账号状态、AT/RT、上游 401/403、代理 | 修复凭据/代理；导入或刷新；不要盲目批量删除 |
| 可续期占比低 | RT 有效率、刷新任务、上游限流 | 刷新 RT；重新导入有效账号；记录失败原因 |
| 队列积压 | `queued_tokens`、workers、失败率、上游延迟 | 降业务量/扩并发/修代理；必要时取消任务 |
| 请求 503 增多 | `admission.queued`、CPU/内存、上游慢 | 扩容或降载；客户端按 Retry-After 重试 |
| 备份失败 | R2 凭据/网络、磁盘、`openssl`、口令 | 修复后立即重跑；连续失败按红色处理 |
| 容器不健康 | `ps`、`RestartCount`、入口日志、磁盘 | 按第 8 节；必要时回滚 |
| 数据库碎片高 | `fragmentation_percent`（数据库状态） | 低峰期「一键修复」；先备份 |

## 8. 常见故障

### 8.1 `unable to open database file`

按顺序排除，不要先删库：

1. **入口是否修正过权限**：
   ```bash
   docker compose -f "$COMPOSE_FILE" logs app | \
     grep -E '修正数据目录权限|chown 失败|不可写|启动：DATA_DIR'
   ```
   有 `修正数据目录权限` 且写测试通过，说明权限已修好；有 `chown 失败` 看第 4 步。
2. **应用真实 UID**：
   ```bash
   docker compose -f "$COMPOSE_FILE" exec app sh -c 'grep ^Uid /proc/1/status'
   ```
   不是 10001 时，检查 compose 是否覆盖了 `user:`。
3. **10001 写测试**：
   ```bash
   docker compose -f "$COMPOSE_FILE" exec -u 10001 app sh -c \
     'touch /data/.write-test && rm /data/.write-test && echo WRITABLE'
   ```
4. **宿主属主/只读/磁盘**：
   ```bash
   stat -c '%u:%g %a %n' data data/chatgpt2api.db data/chatgpt2api.db-wal 2>/dev/null
   findmnt -no TARGET,FSTYPE,OPTIONS -T /opt/qingmeng/data 2>/dev/null
   df -h /opt/qingmeng/data; df -i /opt/qingmeng/data
   ```
5. **路径类型**：
   ```bash
   ls -ld data data/chatgpt2api.db
   ```
   `chatgpt2api.db` 应是文件；如果是目录，先备份现场再处理。
6. **`DATABASE_URL`**：设置了非 SQLite URL 时，先确认连接串、网络和 `pg_isready`；
   SQLite 绝对路径是 `sqlite:////data/chatgpt2api.db`（四个斜杠）。
7. **数据库损坏**：用 `pragma integrity_check`；失败时按第 6 节还原备份，不要直接删除。

修复权限的兜底：

```bash
docker compose -f "$COMPOSE_FILE" stop app
chown -R 10001:10001 data
chmod -R u+rwX,g+rwX data
chmod 775 data
docker compose -f "$COMPOSE_FILE" up -d
```

NFS/CIFS、只读挂载、SELinux、rootless Docker 的处理见
[deployment.md 3.4](deployment.md#34-数据目录权限自动修正与仍失败时)。

### 8.2 `config.json` 变成目录

原因：旧 Compose 把 `./config.json` 绑定挂载到容器；宿主文件不存在时 Docker 自动创建目录。
应用会警告并忽略该目录，认证回退到环境变量；没有环境变量时应用会报 `auth-key 未设置`
（Compose 通常先在变量插值阶段被 `:?` 拦住）。

检查：

```bash
cd /opt/qingmeng
ls -ld config.json
docker logs --tail=200 qingmeng-gpt | grep -i 'config.json'
```

处理：

```bash
cd /opt/qingmeng
docker compose -f docker-compose.yml down
ls -ld config.json
```

如果它是真实文件并保存了 `auth-key`，先安全读取并写入 `.env`，再移走；只在本机执行，
不要把输出贴到工单或聊天记录：

```bash
python3 -c 'import json; print(json.load(open("config.json"))["auth-key"])'
```

然后二选一：

```bash
# 空目录（Docker 自动创建）：rmdir
# 非空或真实文件：先备份再移走
rmdir config.json
# mv config.json "config.json.bak-$(date +%F-%H%M)"
```

确认 `.env` 后启动：

```bash
grep -c '^QINGMENG_AUTH_KEY=.' .env
chmod 600 .env
docker compose -f docker-compose.qingmeng.yml up -d
```

精简 Compose 不挂载 `config.json`，切过去后不会复发。详见
[deployment.md 8.2](deployment.md#82-configjson-变成目录)。

### 8.3 DNS

分层判断：

```bash
# 容器内
docker compose -f "$COMPOSE_FILE" exec app getent hosts chatgpt.com
docker compose -f "$COMPOSE_FILE" exec app \
  curl -sS -o /dev/null -w '%{http_code}\n' https://chatgpt.com

# 宿主机
cat /etc/resolv.conf
getent hosts chatgpt.com
systemctl status systemd-resolved 2>/dev/null

# 应用的三条链路
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/upstream-diagnostics" \
  | python3 -m json.tool
```

处理顺序：

1. 宿主 DNS 坏：修 `/etc/resolv.conf`，或改用静态 `nameserver 223.5.5.5/1.1.1.1`；
2. 容器 DNS 坏、宿主正常：确认 Compose 的 `dns:` 生效，`up -d --force-recreate`；
3. 入口日志出现 `DNS 仍不可用`：容器可离线启动，但上游会失败；看宿主机/安全组；
4. 构建阶段失败：`docker build --network host`，或配 `NPM_REGISTRY` / `PIP_INDEX_URL`；
5. 只有代理节点失败：宿主/容器 DNS 可能正常，按 8.4 查节点。

`/etc/resolv.conf` 在容器创建时生成，宿主机修好后必须重建容器，`restart` 可能不够。
详见 [deployment.md 3.8](deployment.md#38-dns-解析异常)。

### 8.4 代理 `direct` / `direct (proxy group degraded)`

先分清：

- `direct`：没有配置代理，或明确指定直连；如果上游允许直连，这是正常配置；
- `direct (proxy group degraded)`：代理组有节点但全部熔断/冷却，系统回退直连；
  如果上游拦截直连，请求会失败，必须修代理。

检查：

```bash
curl -fsS -H "Authorization: Bearer $AUTH_KEY" "$BASE_URL/api/system/proxy-health" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("counts",d["health"]["counts"]); print("fallback",d["fallback_reference"]); print([(n["label"],n["state"],n.get("cooldown_remaining")) for n in d["health"]["nodes"]])'

curl -fsS -X POST -H "Authorization: Bearer $AUTH_KEY" \
  "$BASE_URL/api/system/proxy-health/probe" | python3 -m json.tool
```

页面：「设置 → 智能调度与维护 → 代理健康与自动切换」。

处理：

1. 代理组为空：先加节点；没有节点时不是“回退直连”，而是组不可用；
2. 节点全部 `down`/冷却：查节点 URL、认证、DNS、出口 IP 是否被封；等冷却或换节点；
3. 账号绑定了别的组：在账号/账号组页确认 `proxy_group_id`；
4. 全局默认出口被设为 `direct`：在代理页改回健康组；
5. 直测节点（把 `$PROXY_URL` 换成实际节点 URL）：
   ```bash
   curl -x "$PROXY_URL" -I https://chatgpt.com --max-time 15
   ```
6. 修好后「立即探测」，再发请求，确认 `egress_label` 变为 `proxy:...`；
7. 只有图片请求走直连时，查节点「图片并发」配额是否占满（0 表示不限）。

### 8.5 端口映射

```bash
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" port app 80
ss -lntp | grep ':3100' || true
curl -v http://127.0.0.1:3100/version
curl -v http://<公网IP>:3100/version
```

| 现象 | 处理 |
| --- | --- |
| 无端口映射 | 确认启动的是精简 Compose；检查 `ports` |
| 本机通、外网不通 | 宝塔「安全」、云安全组、`ufw`/`firewalld` 放行 |
| 端口被占用 | `ss -lntp` 找进程；改 `QINGMENG_PORT` 或停冲突服务 |
| 改了端口不生效 | `up -d --force-recreate` |
| 域名 502 | 反代目标应为 `127.0.0.1:3100`；先确认本机 curl 通 |
| 上传 413 | Nginx `client_max_body_size` 太小，按部署文档 3.6 放大 |
| 长请求 504 | 反代 `proxy_read_timeout` 太短；关闭 `proxy_buffering` |
| 页面 404、`/version` 正常 | 镜像缺 `web_dist/index.html`，换完整镜像/重建 |

端口默认值：精简 Compose 3100；默认 `docker-compose.yml` 3000；`deploy-qingmeng.sh`
默认 3000。以 `docker compose port app 80` 为准，不要凭旧文档猜。

### 8.6 其他故障速查

| 现象 | 排查 |
| --- | --- |
| Compose 报 `QINGMENG_AUTH_KEY is missing a value` | `.env` 密钥为空；填 32 字节以上随机串 |
| 登录 401 | 密钥不一致；改 `.env` 后 `up -d` 重建 |
| 容器反复重启 | 入口日志的权限/密钥/DNS/数据库错误；健康检查失败不会触发重启 |
| 队列长时间不动 | workers、`queued_tokens`、上游 429/503、代理；可取消任务 |
| 调度不恢复 | `restore_in_seconds`、阈值、手动锁定；负载均值不触发降档 |
| 检测慢 | 上游限速/代理延迟；看队列速率与失败率 |
| 数据库锁 | SQLite 单写者；降并发、低峰维护；大池切 PostgreSQL |
| 磁盘增长快 | 清 `imports`、`backups`、旧图片/调试日志；检查保留期 |
| R2 备份失败 | 凭据/Bucket/网络/`openssl`/口令；看最近错误 |
| 旧库导入账号少 | 旧库快照缺 WAL；停旧实例后重新复制；已存在账号会跳过 |
| 旧库上传 400 | 不是 SQLite、缺 `accounts` 表、超过 2 GB；看页面错误 |
| PostgreSQL 连接失败 | `DATABASE_URL`、`pg_isready`、网络/密码；本地备份会跳过 |

## 9. 升级 / 回滚速查

完整步骤见 [deployment.md 第 5 节](deployment.md#5-升级与回滚)。

升级前：

```bash
curl -fsS "$BASE_URL/version"
# 页面：R2/本地备份立即备份；记录当前 QINGMENG_IMAGE 与镜像 ID
# 停容器打包 data（需要覆盖图片/文件时）
```

升级：

```bash
cd /opt/qingmeng
# 编辑 .env：QINGMENG_IMAGE=qingmeng/qingmeng-gpt:1.1.8
# 先从仓库 pull，或 docker load 目标 tag
docker compose -f "$COMPOSE_FILE" config --quiet
docker compose -f "$COMPOSE_FILE" up -d --no-build
curl -fsS "$BASE_URL/version"
docker compose -f "$COMPOSE_FILE" ps
```

验证：dashboard `healthy`、账号数一致、队列正常、抽一条请求、备份仍成功。

回滚：

```bash
# 编辑 .env：QINGMENG_IMAGE 改回上一个 tag
# 确认旧镜像仍在本机；否则先 docker load 旧镜像
docker compose -f "$COMPOSE_FILE" up -d --no-build
curl -fsS "$BASE_URL/version"
```

如果数据库结构已变化，先停容器、恢复升级前备份，再启动旧镜像；不要假设旧镜像能读新库。
保留旧镜像和升级前备份至少 7 天，确认稳定前不要删除 `data.failed-*` 现场。

## 10. 交接记录模板

```text
时间：
版本 / 镜像 ID：
变更内容：
影响范围：
备份文件 / 位置：
验证证据（命令与结果）：
回滚点（镜像 tag + 备份）：
遗留问题：
决定人 / 执行人：
```
