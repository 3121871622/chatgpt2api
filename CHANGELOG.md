# 更新日志

> 倾梦GPT绘画企业级百万号池系统 · 独立二开维护
> 版本规则：从 1.0.0 起 —— 日常新增功能、优化、修复 → 递增**小版本号**（1.0.0 → 1.0.1 → 1.0.2 …）；
> 架构级改造、不兼容变更等**大更新** → 递增**大版本号**（1.0.x → 2.0.0）。
> 每次改动都必须在下方新增条目，不允许只改代码不记日志。

> 更新时间精确到分钟；历史条目按镜像构建时间回填，缺少构建记录的旧版本分钟位记为 00:00。

## [1.9.10] 2026-11-13 08:30

紧急修复 1.9.9 引入的图片全量失败事故（image_poll_timeout）。

### 修复

+ [修复] 1.9.9 的 Go 轮询快速路径向上游请求对话文档时只带了精简请求头，缺少上游必需的 `X-OpenAI-Target-Path` / `X-OpenAI-Target-Route` 及浏览器指纹头，上游拒绝后 Go 空转至 60 秒超时；同时该路径失败后直接抛 `ImagePollTimeoutError`，导致 Python 原有轮询路径完全没有机会执行。结果 01:00–08:15 图片调用近乎全失败（每小时失败 45–298 笔）。
+ [修复] 轮询快速路径改用 `self._headers(conv_path)` 的完整上游请求头集合（含 target path/route 与全部指纹头）。
+ [修复] Go 轮询未返回结果时不再短路，回落前重置计时起点，让 Python 轮询仍获得完整预算。

### 变更

+ [变更] Go 上游代理改为显式开关，默认全部关闭：`UPSTREAM_GATEWAY_STREAM=1` 才启用 SSE 流代理，`UPSTREAM_GATEWAY_POLL=1` 才启用轮询代理。未设置时行为与 1.9.8 完全一致。
+ [变更] 新增 `upstream_gateway_client.stream_enabled()` / `poll_enabled()`，替代原先只要配置了 URL 或 token 就启用的行为。

### 验证

+ Python：637 passed，1 skipped。
+ 运营机回滚验证：08:15 前每 5 分钟失败 19–27 笔、成功 0–2 笔；回滚后 08:15 起成功 6 笔、失败 0 笔，恢复 100%。

## [1.9.9] 2026-11-13 00:30

Go 上游代理：SSE 流转发 + 图片轮询，释放 Python 线程。

### 新增

+ [新增] gateway/internal/upstream/proxy.go：Chrome110 TLS 指纹 SSE 流代理（POST /v1/upstream/stream）和对话文档轮询（POST /v1/upstream/poll），Python 侧所有业务逻辑不变，只把网络 IO 委托给 Go。
+ [新增] services/upstream_gateway_client.py：Python HTTP 客户端，UPSTREAM_GATEWAY_TOKEN 未设时完全跳过，行为与现在一致。
+ [新增] gateway/cmd/gateway main.go 加入 /v1/upstream/stream 和 /v1/upstream/poll 路由，复用 GATEWAY_TOKEN 鉴权（X-Gateway-Token 头），读写超时扩大到 315 秒以支持长 SSE 流。
+ [新增] gateway go.mod 加入 bogdanfinn/tls-client v1.18.1 和 bogdanfinn/fhttp（Chrome TLS 指纹），与 Python curl_cffi 对齐。

### 优化

+ [优化] openai_backend_api.py stream_conversation：payload+headers 准备好后优先走 Go 网关，失败透明回退 Python 路径。
+ [优化] openai_backend_api.py _poll_image_results：无 initial_ids 时走 Go goroutine 轮询，释放 Python 线程。

### 验证

+ Python：637 passed，1 skipped；Go：vet + build + 测试通过。

## [1.9.8] 2026-11-12 16:30

Py+Go 混合架构：Go 选号网关原型，Python 客户端接入，PostgreSQL store 层。

### 新增

+ [新增] gateway/ — Go 1.26 选号租约服务：
  - internal/scheduler：账号索引、SWRR 轮转、评分（freshness + latency + health）、租约并发上限、12 个单元测试。
  - internal/store：PostgreSQL 账号持久化层（auto-schema，upsert/delete/loadAll/countByStatus）。
  - cmd/gateway：HTTP 网关（acquire / release / upsert / remove / stats），Bearer 鉴权，启动时从 PG 加载账号；设置 DATABASE_URL 启用 PG，否则纯内存运行。
  - 共 12 个 Go 测试，vet + build 通过。
+ [新增] services/gateway_client.py — Python 侧 HTTP 客户端，透明接入选号主循环；GATEWAY_TOKEN 未设置时完全跳过，不影响现有路径；10 个测试。
+ [新增] Dockerfile 加入 gateway-build 阶段（golang:1.26-alpine，静态链接），编译出的 /usr/local/bin/qingmeng-gateway 随镜像分发。

### 优化

+ [优化] _acquire_next_candidate_token：GATEWAY_TOKEN 存在时优先向 Go 网关发起 acquire（锁外 HTTP，< 1 ms），成功直接返回，失败透明回退本地索引，不改变原有行为。
+ [优化] release_image_slot：锁释放后通知网关 release（best-effort，失败不抛）。

### 验证

+ Python：637 passed，1 skipped；Go：12 passed，vet + build 通过。

## [1.9.7] 2026-11-12 15:10

限流账号同步按恢复时间跳过，减少账号锁排队。

### 背景

+ 1.9.6 的账号锁诊断显示，慢等待几乎都出在 _update_account_impl（写库发生在全局锁内），最大等待 9.1 秒。
+ 后台 account-watcher 每个周期都会对全部限流账号（约 300–490 个）重新查询并写回，不看上游给出的 restore_at。

### 新增

+ [新增] gateway/：Go 选号租约服务原型（索引、评分、HTTP 接口及测试），尚未接入主流程，运行时不受影响。

### 优化

+ [优化] list_limited_tokens 跳过 restore_at 仍在未来的限流账号：没到恢复时间不再重复远程查询和写库；恢复时间已过或缺失的照常同步，待验证账号仍然排除。

### 验证

+ 新增 5 条限流账号恢复时间过滤测试；全量 Python 测试：627 passed，1 skipped；Go：vet 与测试通过。

## [1.9.6] 2026-11-12 13:20

账号锁诊断：定位选号同一瞬间集体卡住 40–80 秒的原因（只加观测，不改选号行为）。

### 背景

+ 1.9.5 上线后，日志里多笔请求在同一毫秒拿到号，等号 47–71 秒，说明它们在等同一个东西。
+ 选号与账号写库共用全局锁 self._lock（_image_slot_condition 与它是同一把锁），
  但还没有数据证明是谁占着这把锁。本地实测 1 万账号全量对比仅 14ms，写库等待与快照重载仍待证实。

### 新增

+ [新增] services/lock_diagnostics.py：带计时的锁 InstrumentedLock，可替换 threading.Lock，
  同时可作为 Condition 的底层锁；记录等待时长、持有时长、持有者函数名与行号。
+ [新增] 等待或持有超过 1 秒写 account_lock_slow 告警日志，内容含阻塞者（blocked_by）。
+ [新增] /api/system/load 增加 account_lock：最大等待、最大持有、慢等待/慢持有次数、当前持有者、最近 20 条慢事件。

### 验证

+ 新增 5 条锁测试：慢持有、慢等待并指出阻塞者、快速使用不报告、非阻塞获取、可作为 Condition 的锁。
+ 全量 Python 测试：622 passed，1 skipped。

## [1.9.5] 2026-11-12 11:10

选号并行预检：压缩等号时间。

### 背景

+ 1.9.4 之后等号仍在 20–30 秒：主循环先 3 选 1 打分，再对选中的一个做串行轻量预检，
  每个死号约 5 秒，最多试 4 个，最坏情况接近 30 秒的选号上限。

### 优化

+ [优化] 3 个候选现在**一起验证**：先在候选里找“最近 120 秒内已验证过”的号，
  有就直接用，完全不发网络请求；没有则对全部候选并行做轻量 `/me` 预检，取最先通过的，其余释放。
  等号时间由“逐个累加”变成“取最快的健康号”。
+ [优化] 并行预检失败的号仍会记录不可用；401 / token_revoked 照旧走失效清理。
+ [保留] 有候选不能轻量预检（额度未知或类型不匹配）时，仍走原来的串行路径，行为不变。

### 验证

+ 新增 3 条测试：并行取最快通过的、有新鲜缓存时零网络请求、全部失败时释放全部槽位并抛出选号失败。
+ 全量 Python 测试：617 passed，1 skipped。

## [1.9.4] 2026-11-12 00:55

调度 3.0：号池均衡使用 + 选号提速（深度优化）。

### 根因（现场只读诊断 10,279 个账号）

+ 7,469 个用过，**2,810 个健康、有额度、在索引里却从未被选中**；
  它们和被用的账号在同一个 free/web、free/codex 桶里，不是被过滤掉的。
+ 选号队列是 FIFO，每次重启/快照重载都按数据库插入顺序重建，
  轮转回到同一批“头部”账号；频繁部署下头部 ~7k 号被反复用到额度耗尽，
  尾部 ~2.8k 号永远轮不到。这就是“1 万号一天不到 1 万张图却额度紧张”。

### 优化

+ [优化] 索引重建改为**最久未用优先（LRU）**：从未用过的账号排最前，
  其余按 `last_used_at` 从旧到新，同级随机打散，重启不再重置轮转。
+ [优化] 评分新增 `freshness` 公平项：从未用/闲置 ≥24h 的账号最多加权 35%，
  足以压过老号约 10% 的健康优势；刚用过的号不加权。
+ [优化] 候选从 2 选 1 改为 3 选 1（P3C），新号进入比较的概率更高。
+ [优化] 轻量 `/me` 预检失败后直接换号，不再追加最长 8 秒的完整远程检查；
  单个死号成本从约 13 秒降到约 5 秒，401/token_revoked 仍走失效处理。
+ [优化] 远程校验缓存从 20 秒延长到 120 秒，减少一张图 60 秒内的重复 `/me`。
+ [优化] 索引扫描预算 8 → 32，避免队头被排除/占用的号挤空一次取号。
+ [新增] `/api/system/load` 增加 `account_index`（就绪数、桶数、在途、扫描量）。

### 验证

+ 全量 Python 测试：614 passed，1 skipped。
+ 新增调度公平测试：时间格式解析、闲置加权、LRU 入队、新号胜过近用老号、
  轻量预检失败不再追加完整检查。

## [1.9.3] 2026-11-11 23:05

调度索引排除已耗尽额度账号（绘画变慢修复）。

### 修复

+ [修复] `classify_account_for_index` 的 image 就绪判断加入额度条件：
  已知 `quota=0` 的账号不再进入图片选号索引；
  `image_quota_unknown` 仍保留，避免误杀未知额度账号。
+ [修复] 此前索引仍会发放已耗尽账号，每次请求要串行远程校验
  多个 quota=0 账号（现场单笔 account_wait 20–70 秒），
  现在直接从索引层过滤，选号不再为死额度账号付出 8 秒/个的远程检查。
+ [优化] 账号额度更新时通过现有 `add` 重新分类，额度恢复后自动回到索引。

### 验证

+ 全量 Python 测试：608 passed，1 skipped。
+ 新增 quota=0 / quota>0 / 未知额度三种索引分类测试。

## [1.9.2] 2026-11-11 21:10

Free 图像额度回复识别修复（1 项 502 堆积修复）。

### 修复

+ [修复] ChatGPT 返回“你已达到 Free 套餐的**图像**生成请求上限”时，
  旧逻辑只识别“图片”，未识别“图像”，把它误判为 `image_tool_error`
  502，继续切换账号并重复消耗额度。
+ [修复] 工具消息 `system_error` 现在也会先检查额度文本；
  命中后归类为 `image_quota_exhausted`（429），账号进入额度冷却并切换，
  不再把整池 Free 额度耗尽表现成 502。
+ [修复] 对应增加“图像生成请求上限”中文措辞与 tool system_error 单元测试。

### 验证

+ 全量 Python 测试：605 passed，1 skipped。
+ 现场 20:33–20:58 的 4 笔 502 均由该文本误判造成，已按此路径修复。

## [1.9.1] 2026-11-10 03:00

记忆批量动作补 AT 刷新与逐条删除，任务列表增量刷新（3 项修复）。

### 修复

+ [修复] 记忆开启/关闭/清空执行前先 `ensure_access_token`，
  避免使用已过期 AT 导致整批 `token_revoked` 401。
+ [修复] 记忆清空改为先读取 memories，再逐条
  `DELETE /backend-api/memories/{id}`；集合 DELETE 返回 405 时不再误判
  为成功，空列表直接视为已清空。
+ [修复] 账号清空/开启/关闭结果进入统一进度抽屉，
  并保留“记忆已清空/记忆开/记忆关”状态投影。

### 优化

+ 队列轮询并发保护与 3 秒刷新、账号操作进展 `after_sequence` 增量事件
  已在 1.9.0 生效，本轮继续验证。

### 验证

+ 全量 Python 测试：603 passed，1 skipped；前端构建通过。
+ 实测开启记忆成功；清空接口 405 后改为逐条删除路径。

## [1.9.0] 2026-11-10 02:45

账号记忆批量动作、任务进度实时化与统一后台预算（3 项功能/优化）。

### 新增

+ [新增] 账号批量处理新增“批量开启记忆 / 批量关闭记忆 / 批量清空记忆”：
  - 开启/关闭走 `m3m=true/false` 并回读校验；
  - 清空调用 `/backend-api/memories` 并回读剩余数量；
  - 操作进入统一进度抽屉，显示每条账号结果。
+ [新增] `services/task_scheduler.py`：统一计算后台预算
  `总槽位 - 前台预留 - 当前绘画在途`，队列 worker 和记忆 worker
  都从该预算取线程数；绘画在途高时自动收缩后台，不再固定抢线程。

### 优化

+ [优化] 账号操作进度轮询支持 `after_sequence` 增量事件，
  避免每次拉取全部事件造成“任务列表看着卡住”。
+ [优化] 维护页队列轮询增加并发保护（上一轮未返回不再叠加请求），
  轮询间隔缩短到 3 秒，并显示最近更新时间。
+ [优化] 账号状态新增“记忆已清空”“记忆开”等投影。

### 验证

+ 全量 Python 测试：603 passed，1 skipped；前端构建通过。

## [1.8.0] 2026-11-10 02:10

调度 2.0：延迟感知与能力硬过滤（2 项算法优化）。

### 优化

+ [优化] 账号评分加入 `image_latency_ewma_ms` 延迟分：
  成功请求耗时按 EWMA 更新，选号时快号获得更高权重；默认 45 秒为中性值。
+ [优化] GIF / file_upload 能力改为**已知能力硬过滤**：
  账号已有能力记录但缺少所需能力时，本地直接排除并释放槽位，
  不再发起注定失败的上游请求；最多尝试 100 个候选后返回能力不足。
  未建立能力记录的新账号仍允许探索，避免新池被误杀。
+ [优化] 保持健康分、额度紧迫度、套餐权重、Thompson Sampling、
  出口健康和 5% 探索共同参与评分。

### 验证

+ 全量 Python 测试：599 passed，1 skipped。
+ 延迟 EWMA、硬过滤路径已加测试。

## [1.7.4] 2026-11-10 00:35

修复 multipart `/v1/images/edits` 丢失 output_format 字段（1 项修复）。

### 修复

+ [修复] `parse_image_edit_request` 的 multipart 字段白名单漏了
  `output_format` / `output_compression`，导致编辑接口传了格式仍返回 PNG；
  已补齐并实测编辑请求返回 JPEG。

### 验证

+ 全量 Python 测试：598 passed，1 skipped。

## [1.7.3] 2026-11-10 00:15

图片接口兼容 OpenAI `output_format` / `output_compression`（1 项新增）。

### 新增

+ [新增] `/v1/images/generations` 与 `/v1/images/edits` 支持：
  - `output_format`：`png` / `jpeg` / `webp`（`jpg` 自动归一为 `jpeg`）；
  - `output_compression`：0–100，用于 JPEG/WebP 质量。
+ [新增] 不传 `output_format` 时完全保持原有行为（PNG/GIF 原样返回）；
  传了才在上游返回后本地转换，响应 `mime_type` 与实际文件一致。
+ `svg` 明确返回 400，不做静默忽略。

### 验证

+ 全量 Python 测试：598 passed，1 skipped。
+ 新增 PNG→JPEG/WebP、透明通道、无参数保持原图测试。

## [1.7.2] 2026-11-09 12:40

修复上游 413 被误判为账号故障并跨号重试（1 项修复）。

### 修复

+ [修复] `/backend-api/f/conversation` 返回 413 时归类为
  `image_payload_too_large` 请求级失败：不再切换账号、不标记账号故障，
  直接向客户端返回“图片或请求体过大”的明确提示；避免一次大图请求
  连换 4 个账号、耗时 50 秒并制造多条 413 失败记录。

### 验证

+ 全量 Python 测试：594 passed，1 skipped。

## [1.7.1] 2026-11-07 11:35

账号状态加入记忆标识，额度增加分布统计（2 项优化）。

### 优化

+ [优化] 账号列表状态列显示“正常 / 记忆关”“正常 / 记忆失败”
  “正常 / 记忆开”“正常 / 记忆未检测”；失败状态标红。
+ [优化] 账号统计新增 `quota_distribution` 与 `low_quota_count`：
  按 0、1–5、6–20、21+ 分桶，便于解释“7 千账号为什么只有 7 万额度”。

### 验证

+ 全量 Python 测试：592 passed，1 skipped；前端构建通过。

## [1.7.0] 2026-11-07 10:30

队列闭环、密钥用量账本、记忆失败闭环、Thompson 调度与巡检工具（7 项）。

### 新增

+ [新增] 账号操作进度携带 `job_id`；前端停止按钮真正调用
  `/api/system/queue/{job_id}/cancel`，任务取消后由队列 on_finish 收口。
+ [新增] 密钥图片用量事件账本（最近 100 条）：
  `GET /api/auth/users/{key_id}/usage`，记录 request_id、模型、reserve/refund；
  图片、编辑、聊天生图、Responses 图片工具都计入额度。
+ [新增] 记忆关闭失败账号可重试：
  `POST /api/accounts/memory/retry-failed`；401/token_revoked 自动走失效账号处理；
  设置页“凭据体检”新增重试入口。
+ [新增] Thompson Sampling：维护 Beta 成功/失败参数，选号评分带采样；
  继续保留额度紧迫度、套餐权重和 5% 探索。
+ [新增] 队列面板显示 check / refresh / sync 三条 lane 的积压；
  低可用告警新增队列积压告警（> max(500, 账号总数)）。
+ [新增] `scripts/_load_test_1_7.py` 只读压测脚本，覆盖 version/load/queue/account-list。

### 优化

+ [优化] 图片选号在缓存额度为正时先做一次 `/backend-api/me` 轻量探活，
  避免每次都走 `/me + init + default-account` 三次上游调用。

### 验证

+ 全量 Python 测试：589 passed，1 skipped；前端构建通过。

## [1.6.1] 2026-11-07 02:30

调度“额度紧迫度/套餐权重/探索”与设置页排序（2 项优化）。

### 优化

+ [优化] 选号评分加入额度紧迫度（离重置越近、剩余额度越高，优先级越高）、
  套餐价值权重（Plus/Team/Pro 小幅加权）和 5% 探索：避免低分账号被永久
  冻结，也给新号积累健康分的机会。
+ [优化] 设置页把“用户密钥与额度”前移到第二位，并改名为
  “用户密钥与额度”，维护类功能继续留在“智能调度与维护”。
+ 全量 Python 测试：588 passed，1 skipped；前端构建通过。

## [1.6.0] 2026-11-07 02:10

应用密钥批量管理、封禁与图片额度（1 项新增、2 项优化）。

### 新增

+ [新增] 用户密钥支持每日/每月图片额度与到期时间：
  - `image_daily_limit` / `image_monthly_limit`，0 表示不限；
  - 每次 `/v1/images/generations`、`/v1/images/edits` 预留额度，失败退款；
  - 超限返回 `429 insufficient_quota`；
  - 到期后密钥直接失效。
+ [新增] 密钥批量接口 `POST /api/auth/users/batch`：批量启用/禁用/删除、
  设置日限/月限/到期时间。

### 优化

+ [优化] 密钥管理面板支持全选、批量启用/禁用/删除、批量设置日限/月限，
  并在列表直接展示今日/本月用量与到期时间。
+ 全量 Python 测试：586 passed，1 skipped；前端构建通过。

## [1.5.0] 2026-11-07 01:30

AT 刷新/配额同步接入统一任务队列（1 项架构优化）。

### 优化

+ [优化] `/api/accounts/sync`、`/api/accounts/refresh`、
  `/api/accounts/refresh-access-token` 不再各自创建临时线程池，统一提交到
  `AccountJobQueue` 的 `sync` / `refresh` lane：
  - 系统维护页的异步队列能看到真实任务、总数、进度和失败数；
  - 与账号检测共享 128 个 worker，不再额外堆线程池；
  - 进度按 token 实时更新，前端轮询能看到持续变化；
  - 任务失败会进入队列错误列表，不再静默丢失。

### 验证

+ 全量 Python 测试：583 passed，1 skipped。
+ 部署后用小批量账号实际验证 sync / refresh 进度推进。

## [1.4.1] 2026-11-07 01:20

批量任务预留线程、旧库清空与进度上限修正（3 项修复/优化）。

### 优化

+ [优化] 账号维护任务固定为前台绘画/文本预留最多 20 个处理槽位：
  `foreground_reserve(512)=20`，小池按 20% 比例保留。
+ [优化] 旧数据库上传列表新增“清空全部”，后端新增
  `DELETE /api/system/legacy-sync/imports`，并写审计日志。
+ [修复] 账号批量同步/AT 刷新进度条不再用“当前选择数”截断后端进度；
  select-all 场景会按后端权威 total 显示，避免提前 100% 或卡住。

### 验证

+ 新增 `foreground_reserve` 回归测试；前端构建通过；
+ 全量 Python 测试：583 passed，1 skipped。

## [1.4.0] 2026-11-07 00:30

账号记忆批量关闭（1 项新增、1 项自动化）。

### 新增

+ [新增] `POST /api/accounts/memory/disable` 批量关闭 ChatGPT 网页记忆：
  对每个账号 PATCH `m3m=false`，再读取 `settings/user` 校验
  `m3m/sunshine/moonshine` 均为 false；不修改 `m3m_opted_out`。
  批量任务固定 **10 个 worker**，给前台绘画请求预留处理能力；
  进度走现有 `/api/accounts/operations/{progress_id}`。

### 自动化

+ [自动化] 新导入账号（JWT 格式的 access token）保存后自动进入
  后台关闭记忆队列；已关闭的账号跳过；失败写入账号字段
  `memory_disable_result` / `memory_disable_error`，可在后续批量中重试。

### 验证

+ 新增账号记忆开关单测；全量 Python 测试：582 passed，1 skipped。

## [1.3.2] 2026-11-06 08:10

短 base64 图片上传误判路径修复（1 项修复）。

### 修复

+ [修复] 小尺寸图片（64×64 等）的 base64 字符串不足 512 字符时，被
  `_decode_image_base64` 当成"本地文件路径"执行 `Path.exists()`，触发
  `[Errno 36] File name too long`，导致 `/v1/images/edits` 连续 500。
  现在只有带图片后缀、basename ≤ 240 的短字符串才会尝试本地路径，
  纯 base64 直接解码；上传文件名也截断到 128 字符，避免超长文件名。
+ 新增 2 个回归测试：短 base64 不被当路径、本地图片路径仍可用。
+ 全量 Python 测试：580 passed，1 skipped。

## [1.3.1] 2026-11-06 03:30

调度可观测性（1 项优化）。

### 优化

+ [优化] `/metrics` 增加上游熔断状态、滚动失败率、熔断窗口样本数；
  `/api/system/load` 增加 `upstream_circuit` 与 `egress` 健康分快照，便于
  Prometheus/Grafana 或人工巡检定位“是号坏了还是出口/上游坏了”。
+ 全量 Python 测试：578 passed，1 skipped。

## [1.3.0] 2026-11-06 03:20

智能调度升级（健康分、配额/能力/出口评分、上游熔断、轻量检测）。

### 优化

+ [优化] 图片选号加入账号健康分：按成功 EWMA、429、超时、剩余额度和
  慢流/失败冷却计算分数；慢号、限流号、低额度号会自然降权。
+ [优化] 选号改为 Power of Two Choices：每次租用两个候选，保留分数更高的
  一个并立即释放另一个，不再对 8,000 个账号做全量排序。
+ [优化] 配额与能力参与评分：额度高的账号权重更高；账号成功出图后会记录
  `model:*`、`gif`、`file_upload` 等能力，后续同类请求优先选择有成功记录的
  账号，能力未知时仍可回退，不硬性拒绝。
+ [优化] 增加出口健康分：按代理出口统计成功/429 EWMA，出口异常时降低该出口
  账号的优先级；请求级错误（内容违规、参数错误）不再污染账号和出口健康分。
+ [优化] 新增全局上游熔断：30 秒窗口内图片失败率达到阈值时，选号快速失败，
  不再连切多个账号把健康号打成待验证；熔断冷却后自动半开恢复。
+ [优化] 账号检测改为轻量探测：批量检测只打 `/backend-api/me`，不再每个账号
  请求 `/me`、`conversation/init`、`accounts/check` 三个接口；额度同步仍走
  完整刷新接口。
+ [优化] 图片最大切号次数改为读取 `image_max_account_attempts` 配置，不再
  代码里固定 8 次。

### 验证

+ 新增健康分、能力评分、出口脱敏、熔断器、请求级失败降权测试。
+ 全量 Python 测试：578 passed，1 skipped。

## [1.2.10] 2026-11-06 02:20

1.2.9 图片总额统计回归修复（1 项修复）。

### 修复

+ [修复] 1.2.9 把 `AccountService._is_account_selectable` 当实例方法调用，
  但该方法按静态方法使用，导致 `/api/dashboard` 返回 500
  （`takes 1 positional argument but 2 were given`）。补上 `@staticmethod`
  并新增 `get_stats()` 只统计可调度账号额度的回归测试。
+ 全量 Python 测试：569 passed，1 skipped。

## [1.2.9] 2026-11-06 02:10

账号调度公平性与批量检测安全（3 项优化）。

### 优化

+ [优化] 账号选号从“按 `(plan, source)` 桶等权轮转”改为按桶内可用账号数
  平滑加权轮询。此前 2 个 Plus 账号会和 8,000 个免费账号的桶各拿一份流量，
  实测两个 Plus 号占 459 次图片调用的 15.9%；现在小桶不再被过度使用，
  单账号使用频率更均匀。
+ [优化] `/api/accounts/check` 批量检测改为只读：显式 `remove_invalid=False`，
  检测本身不再删除失效账号；清理继续走独立的预检/确认接口，避免“点一下检测
  就批量删号”。
+ [优化] 图片总额统计只汇总真正可调度的账号（排除 AT/RT 失效、pending、
  禁用等），不再把死号额度算进 `total_quota`；清理前虚高的 90,972 会回到
  实际可用额度。

### 验证

+ 新增桶权重回归测试：2 个 Plus + 8 个免费账号的 1,000 次选择中，Plus
  占比约 20%，不再接近 50%。
+ 全量 Python 测试：568 passed，1 skipped。

## [1.2.8] 2026-11-05 19:35

批量账号维护阻塞图片选号修正（1 项修复）。

### 修复

+ [修复] 批量账号检测/刷新使用全部账号处理并发时，首个前台图片选号拿不到
  priority 槽：`slot()` 只在已有 priority 请求时才预留 20% 余量，512 个批量
  请求可先占满全部槽位，图片请求只能等批量请求释放。19:23 全量检测期间因此
  出现 20 个 `/v1/images/*` 503 `no_available_account`，单个请求在选号阶段
  等了 141 秒才失败。现在批量路径**始终**只使用 `limit - 20%`，前台图片请求
  始终有可用余量，不再被批量维护饿死。

### 验证

+ 新增并发回归测试：5 个并发槽位下只允许 4 个批量任务同时运行，priority
  请求可在批量任务仍持有时立即获取槽位。
+ 全量 Python 测试：567 passed，1 skipped。

## [1.2.7] 2026-11-05 17:25

GIF 返回去除封面图（1 项修复）。

### 修复

+ [修复] Images 2.6 的 GIF 请求上游会额外返回一张高清 PNG 封面；此前把 GIF 和封面
  都放进 `data`，`n=1` 会返回 2 个文件，导致下游计费系统按 2 张图重复计费。
  现在只要返回中存在 GIF，就只保留 GIF 资产，封面 PNG 不再保存、不再返回；
  `n=1` 的 GIF 请求固定返回 1 个 `image/gif`。

### 验证

+ 全量 Python 测试：567 passed，1 skipped。

## [1.2.6] 2026-11-05 17:10

Images 2.6 与 GIF 动图支持（1 项新增、2 项优化）。

### 新增

+ [新增] 图片模型加入 `gpt-image-2.6`、`gpt-image-2.6-flare`、`gpt-image-2.6-sunburst`，
  与现有 2.5 系列并存，客户端可直接按模型名请求。

### 优化

+ [优化] GIF 请求会在提示词末尾自动追加“直接生成可循环 GIF、不要向用户提问、不要返回
  `ImagegenAskUserInput` 表单”的指令；实测同样的 GIF 提示词此前会被上游要求补充主题/风格，
  追加后可直接出图。
+ [优化] GIF 返回值按真实格式保存为 `.gif` 并使用 `image/gif`，不再套用 `.png`/`image/png`；
  返回体新增 `mime_type` 字段，GIF 不再经过缩放（避免被压成单帧静态图）。

### 验证

+ 实测 GIF 请求返回 `GIF89a` 字节，保存 URL 为 `.gif`；静态图仍为 PNG，不受影响。
+ 全量 Python 测试：566 passed，1 skipped。

## [1.2.5] 2026-11-05 12:40

图片账号额度耗尽识别修正（1 项修复）。

### 修复

+ [修复] 上游把“每日图片生成额度已用完”作为普通助手文本返回时（如
  `已达到每日图片生成上限，预计 4 小时后恢复`），过去会归类为
  `upstream_text_reply`：不切号、不标记账号，同一个额度耗尽的账号会继续被
  选中并反复失败。现在会识别中英文额度耗尽文案并归类为
  `image_quota_exhausted`，立即切换到下一个账号；候选账号耗尽时按 429
  `insufficient_quota` 返回，不再把额度问题伪装成提示词违规。

## [1.2.4] 2026-11-01 02:05

图片选号远程校验超时修正（1 项修复）。

### 修复

+ [修复] 图片选号调用账号 `/me` 等远程校验时沿用 20 秒单请求超时；当第一个候选账号
  上游响应慢，30 秒选号窗口会被一个账号占满，账号池明明有 9,000+ 可用账号仍返回
  `503 no_available_account`。图片选号现在使用 8 秒单请求校验超时，慢账号会尽快跳过并
  尝试下一个候选，不再让单账号阻塞整个选号。普通账号巡检仍保持 20 秒超时。

## [1.2.3] 2026-10-30 18:27

图片编辑输入校验与调用数量统计修正（2 项修复）。

### 修复

+ [修复] 图生图的图片或蒙版无法被 Pillow 解码时，过去会进入上游上传阶段，
  连续切换账号后返回 `500 internal_error`。现在在选号前校验输入，直接返回
  `400 invalid_image_input`，不再消耗账号、不再伪装成服务端异常。
+ [修复] 调用日志的“请求数量”会被上游额外返回的图片抬高。例如请求 `n=4`，
  上游某个槽返回 2 张，日志会显示 5 个请求。现在请求数量保持用户请求的 4，
  成功数量按实际交付的 5 记录，结果文案显示“生成 5 张图片（请求 4 张）”。
+ [运维] 图片流超时 300 → 180 秒、URL 轮询超时 300 → 120 秒，缩短超慢请求的
  最坏等待时间；正常出图耗时不变。

## [1.2.2] 2026-10-26 23:21

空图片账号冷却（1 项优化）。

### 优化

+ [优化] 图片上游返回 `no_image_generated` 后，该账号进入 10 分钟图片冷却，
  后续图片请求优先避开刚失败的账号，降低连续踩到同一批空结果账号的概率。
+ [说明] 实测同样的“小狗”提示词在运营机可以正常返回图片（200，1.5MB），
  说明并非所有请求都被上游拦截；新增的失败账号冷却用于减少账号级偶发空结果。

## [1.2.1] 2026-10-25 23:25

后台路径改为可隐藏的入口（1 项安全优化）。

### 优化

+ [优化] 后台入口从固定 `/admin` 改为可配置路径，登录页也在该路径的 `#/login` 下；
  `/login` 不再是一个可猜的入口，未命中当前后台路径的页面统一 404。
+ [新增] 系统设置可修改后台路径；修改后旧路径立即失效，页面会跳到新路径。
+ [新增] 固定恢复入口 `/admin-recover`：忘记自定义路径时，用恢复密钥重新设置后台路径。
  该入口只接受恢复密钥，不会暴露后台页面。

## [1.2.0] 2026-10-25 23:04

后台登录与自定义首页（结构性更新）。

### 新增

+ [新增] 根路径 `/` 改为后台可配置的 HTML 首页：在“系统设置 → 自定义首页”编辑并开启后
  直接返回该 HTML；未开启或内容为空时根路径返回 404。后台固定为 `/admin`，登录页固定为
  `/login`，不会因为首页配置而改变。
+ [新增] 自定义首页支持任意 HTML（单文件，最大 512 KB），并提供“打开首页预览”。

### 优化

+ [优化] 更新日志时间精确到分钟；本轮可追溯的版本按镜像构建时间回填，旧版本无构建记录时
  明确标记为 00:00。

## [1.1.9.19] 2026-10-25 22:48

后台登录路径与登录方式（1 项新增）。

### 新增

+ [新增] 后台默认入口改为 `/admin`，访问根路径自动进入后台；登录页保持 `/login`，前端切换为 history 路由。
+ [新增] 支持“密钥登录 / 账号密码登录”两种后台登录方式，默认仅密钥登录；开启账号密码后，
  登录页只显示账号密码，密钥仍保留为恢复凭据。
+ [新增] 系统设置新增“后台登录方式”：可设置管理员用户名、密码，切换登录方式。
+ [新增] 忘记密码时可在登录页用恢复密钥重置密码；重置后使用新密码登录。
+ [说明] 密码使用 PBKDF2-HMAC-SHA256 存储，登录成功后签发 7 天 HMAC 会话令牌，不把密码下发给前端。

## [1.1.9.18] 2026-10-25 22:31

上游 message.error 文本透传（1 项修复）。

### 修复

+ [修复] 上游把失败原因放在 `message.metadata.error` / `message.error` 而不是正文时，
  现在会提取其中的 `message`/`detail`/`reason` 文本，作为 `no_image_generated` 的原始
  详情返回；只有上游确实没有提供任何文本时才使用兜底文案。

## [1.1.9.17] 2026-10-25 22:16

上游文本直接透传（1 项修复）。

### 修复

+ [修复] 上游明确返回文本时（含内容政策说明、无法生成的原因），图片错误响应直接使用
  该文本，不再替换成通用错误；有文本时不再切换账号，无文本的 `no_image_generated`
  才按 1.1.9.16 的规则切号重试。

## [1.1.9.16] 2026-10-25 22:04

图片未出图时切号重试（1 项修复）。

### 修复

+ [修复] `no_image_generated`（上游只返回文字/没有图片结果）不再立即返回 400，
  改为按 `image_account_retry_enabled` 与 `image_max_account_attempts` 切换账号继续生成；
  只有全部账号尝试失败才返回错误。覆盖“返回文字消息”和“完成但没有图片结果”两条路径，
  避免单账号偶发空结果直接暴露给客户端。

## [1.1.9.15] 2026-10-25 20:03

慢图片流账号冷却（小更新，1 项优化）。

### 优化

+ [优化] 单次图片生成 `stream_ms >= 60s` 时记录 `image_stream_slow`，并把该账号
  放入 30 分钟图片冷却（`image_slow_until_ts`）。后续图片选号优先避开刚产生过
  60–97 秒慢流的账号，降低长尾请求概率；冷却到期自动恢复，账号不会被删除或停用。
+ [说明] 超 1 分钟的请求本身多为上游图片流慢（实测选号仅 1–4 秒，流式生成 84–90 秒），
  冷却只能降低再次踩到同类账号的概率，不能改变上游生成速度；大图/多图建议走
  异步图片任务接口 `/api/image-tasks/...`。

## [1.1.9.14] 2026-10-25 18:14

修复 no_image_generated 在调用日志里显示成“文本”而不是失败（1 项）。

### 修复

+ [修复] 图片上游未返回图片（`no_image_generated`）虽然 HTTP 是 400，但调用记录
  之前按 400 归类为 `text_review`，`is_failure=false`，在“失败日志”里看不到。
  现在按图片失败记录为 `failed`，并显示具体原因“上游没有返回图片（可能触发内容政策
  或生成失败）”；仍不切换账号、不重试 4 个号，避免浪费额度。

## [1.1.9.13] 2026-10-25 17:53

图片选号超时放宽到 30 秒（小更新，1 项）。

### 优化

+ [优化] 图片选号硬上限从 20 秒放宽到 30 秒。17:44 出现一次
  `no_available_account`（`image account selection timed out during remote validation`），
  当时号池健康但上游 `/me` 校验变慢；30 秒仍远低于早期几分钟的卡顿，同时减少健康池
  上的偶发超时失败。

## [1.1.9.12] 2026-10-25 12:29

修复 Responses 字符串 input 丢失（1 项）。

### 修复

+ [修复] `/v1/responses` 在 `input` 为纯字符串且带 `tools` 时，`tool_messages_from_input`
  把字符串包成列表后只处理 dict，导致用户原始请求被丢弃，只剩工具契约，模型回
  “What would you like me to work on?”。现在字符串 input 直接进入消息构造，用户请求
  与契约一并保留；auto/required 工具选择都能正常触发 `function_call`。

## [1.1.9.11] 2026-10-25 00:00

修复 Responses/Codex 工具调用不触发（1 项）。

### 修复

+ [修复] `/v1/responses` 的工具轮没有像 chat-completions 那样把
  `AVAILABLE_TOOLS` 契约注入上游消息，模型只看到用户文本，于是回 prose，
  客户端永远收不到 `function_call`。现在复用同一套 `to_upstream_messages`
  + 系统提示注入逻辑；Codex/Cline 等走 Responses 的编程工具可正常发起工具调用。

## [1.1.9.10] 2026-10-25 09:55

后台额度探测（小更新，1 项优化）。

### 优化

+ [优化] 账号守护每轮额外探测 10 个“配额 0 且 12 小时未远程校验/从未校验”的账号，
  刷新实际图片额度。图片选号会跳过已知 quota=0 的账号，没有这个探测，免费号每日
  额度重置后可能永远不再被使用。探测使用 `remove_invalid=False`，不会直接删号。

## [1.1.9.9] 2026-10-25 09:43

图片与缩略图浏览器缓存（小更新，1 项优化）。

### 优化

+ [优化] `/images/...` 与 `/image-thumbnails/...` 增加
  `Cache-Control: public, max-age=31536000, immutable`。图片文件名带哈希，
  可长期缓存，减少重复下载与后端/网关压力。

## [1.1.9.8] 2026-10-25 09:34

账号测活接口防误触（小更新，1 项）。

### 修复

+ [修复] `POST /api/accounts/check` 在 `selection`、`account_ids`、`access_tokens` 全为空时
  以前默认“全选所有账号”，一个格式错误的请求就可能排队 8,000+ 个远程测活。现在返回
  `400 empty_selection`，必须显式传选择范围；网页端不受影响。

## [1.1.9.7] 2026-10-25 09:22

容器优雅停机（小更新，1 项优化）。

### 优化

+ [优化] uvicorn 增加 `--timeout-graceful-shutdown 10`：容器重启时给在途请求最多 10 秒
  收尾时间，配合 Nginx 对 GET 的 502 重试，减少部署瞬间的 502/连接重置。

## [1.1.9.6] 2026-10-25 08:11

修复历史图片 URL 404（小更新，1 项）。

### 修复

+ [修复] `/images/...` 以前只认图库索引里的 `local` 记录；从旧数据目录复制回来的历史图片
  虽然文件存在，仍会 404。现在磁盘上存在且路径合法时直接返回文件，旧分享链接恢复可访问。

## [1.1.9.5] 2026-10-25 07:56

旧库日志批量写入（小更新，1 项优化）。

### 优化

+ [优化] 旧库日志导入改为 **500 条一批、单事务提交**（`CallRecordService.append_items` /
  `CallRecordRepository.append_many`），替代逐条 commit；重复 id 回落为逐条跳过。
  61,751 条日志的导入从“每 5,000 条约 3–7 分钟”降到秒级/批，避免长时间占用数据库写锁。

## [1.1.9.4] 2026-10-25 07:41

图片选号不再卡几分钟（2 项优化）。

### 优化

+ [优化] 本地 `quota <= 0` 的账号不再进入图片候选：此前“正常但额度为 0”的账号会被逐个
  远程预检，20 个账号串行可耗 9 分钟。现在候选池直接跳过已知耗尽账号，没有可用额度时
  快速返回额度耗尽，而不是挂着。
+ [优化] 单次图片选号增加 **20 秒硬上限**、最多 8 个候选：远程网络慢时也不会无限串行，
  超时返回可重试的 503，客户端能及时收到结果。

## [1.1.9.3] 2026-10-25 07:28

修复旧库日志导入在遇到重复行后卡住（小更新，1 项）。

### 修复

+ [修复] 旧库日志同步遇到目标库已存在的 record id（`UNIQUE constraint`）时，原逻辑停在水印处，
  表现为后续每轮 `imported_call_records=0`、`pending_call_records` 不再下降。现在把重复行视为
  “已导入”并继续推进水印，同时记录 `legacy_sync_record_failed` 供排查真正的写入错误。

## [1.1.9.2] 2026-10-25 07:21

图片失败不再报 502、旧库账号测活会刷新 AT（2 项）。

### 修复

+ [修复] 图片生成最终“没有产出图片”（no_image_generated，常见于提示词触发上游内容政策）
  由 502 改为 **400 invalid_request_error**，客户端能看到明确失败而不是反复重试/网关错误。
+ [修复] 旧库同步的自动测活使用 `preflight_refresh=True`：AT 过期但 RT 可用的导入账号
  会先刷新再校验，不再把 1,277 次可恢复账号记成检查失败。

## [1.1.9.1] 2026-10-25 01:53

修复旧库导入丢失 refresh_token（小更新，1 项）。

### 修复

+ [修复] 旧库同步/导入只复制 `access_token`，没有写入 `refresh_token` 与邮箱/身份字段，
  导致导入后的 7,248 个账号全是“AT-only”，AT 到期即失效。现在按完整账号 payload 导入，
  并对已存在但缺 refresh_token 的账号做一次回填（不覆盖已有 RT）。
+ [说明] 回填只补空字段：已有 refresh_token 的账号保持不变。

## [1.1.9] 2026-10-25 01:39

> 发布草稿：本版条目按各工作流汇总；`VERSION`、`pyproject.toml`、`package.json` 与
> compose 镜像 tag 由父级统一更新。前端 P0 / 图库 P1 按
> [`docs/frontend-performance-1.1.8.md`](docs/frontend-performance-1.1.8.md) 的改造
> 清单汇总，以合并后的前端提交为准。

稳定性收口版：账号组与图片选号 500 修复、文本切号重试、视觉识图路由、安全
H-01/H-02/H-03、代理策略/健康、旧库导入加固，以及账号页/图库性能治理。

### 修复

+ [修复] 账号组相关接口全部 500：`_account_group_payload()` 的函数签名在一次编辑中
  丢失，函数体被粘到 `_credential_summary()` 的 `return` 之后成为不可达代码，6 处
  调用直接 `NameError`。恢复签名后 `GET/POST/DELETE /api/account-groups`、
  `POST /api/accounts/group` 以及“创建账号时指定账号组”恢复；随函数体一起失效的
  2 秒组缓存与 `group_counts()` 重新生效，账号页不再每次全池重算。新增账号组
  列表/创建/删除冒烟测试。
+ [修复] 图片选号 500（根因）：池中 free 账号图片额度为 0 时，选号器抛
  `ImageAccountSelectionError`；协议层只映射已知的结构化错误，其余异常落到通用
  请求处理器，被包装为 `internal_error` / HTTP 500。现在选号器在边界把未预期异常
  归一为 `ImageAccountSelectionError("unavailable")`；`quota_exhausted` → 429
  `insufficient_quota`，`unavailable` → 503 `no_available_account`，
  `deadline_exceeded` → 503 `task_interrupted`，并记录
  `image_account_selection_internal_error`。缓存命中前继续校验套餐/来源，池子没号
  不再伪装成服务端 bug。
+ [修复] 文本对话切号重试：首个 delta 之前遇到 401、429、上游 5xx 或连接失败时自动
  换下一个账号（最多 3 个账号）；429 标记“限流”，401 投递 AT 校验；一旦已向客户端
  输出 delta 就停止重试，避免重复内容；全部账号失败时抛原始异常并附带
  `account_email`，保持既有 502 映射。日志只记录邮箱、错误码和尝试序号，不落 token。
+ [修复] 视觉识图被误判为生图：部分 OpenAI 兼容客户端在带图请求里也发送
  `modalities: ["text", "image"]`，旧逻辑据此走 image tool，报
  “The image generation tool encountered an error”。现在先检测 `image_url` /
  `input_image` / `image`，带图请求按视觉对话路由；只有无图且显式要求 image 时才
  生图；图像模型带图仍走编辑。
+ [修复] 调度策略表单：空值、NaN、越界值不再提交到后端；失焦自动把小数收敛为整数；
  保存前校验“恢复阈值必须小于过载阈值”；有未保存修改时切换模式/刷新不覆盖草稿，
  并新增“重新载入”；过期轮询/保存响应不回写，保存/切换期间不拉取策略，避免竞态
  把刚提交的值抹回去。

### 安全

> 对应审计报告：[`docs/security-review-1.1.8.md`](docs/security-review-1.1.8.md)。

+ [安全] H-01 管理员密钥强度：启动时拒绝 `change-me-please`、`changeme`、
  `your_secret_key_here`、`dev-key` 等示例/占位值，并要求至少 12 位（建议 32 位
  随机串）；不满足直接终止启动并给出生成命令。临时测试环境可显式设置
  `QINGMENG_ALLOW_WEAK_AUTH_KEY=1` 放行，`.env.qingmeng.example` 注释同步更新。
+ [安全] H-02 调试包脱敏：`system-info.json` 不再原样导出 `config.data`，改为
  `version / pool / scheduler / settings / account_groups` 白名单投影；`ai_review`
  只保留 `has_api_key`，Cloudflare 只保留 `has_cf_cookies`，备份只保留
  `has_secret_access_key`，账号组只保留 `id/name/enabled/node_count`，不导出代理
  连接串。导出前再跑一次递归脱敏（JWT、Bearer、sk- 密钥、token/password/api_key
  字段、URL userinfo），并保留邮箱便于排障。新增“带真实密钥配置导出后断言不包含
  原值”的回归测试。
+ [安全] H-03 旧库上传鉴权前置：新增请求中间件，在 FastAPI 解析 multipart 之前先
  校验管理员 Bearer，未认证直接 401；`Content-Length` 超过 2GB 直接 413，不再先把
  匿名大文件写入临时目录。endpoint 内仍保留流式读取上限和临时文件清理。

### 优化

+ [优化] 图片轮询提速：SSE 已带 `file_ids` 时初始 settle 收紧到 1 秒、命中确认
  0.8 秒；无初始 ID 的首等待硬上限 3 秒；连续未命中按 1.5 倍退避、上限 12 秒；
  轮询中的瞬时 429 允许 2 次原地重试并遵守 `Retry-After`，不再直接触发换号；
  所有 sleep 都受总超时约束，超时仍返回 `image_poll_timeout`。
+ [优化] 旧库上传进度：改用 XHR `upload.onprogress`，显示真实百分比、已传/总量、
  速率和剩余时间；区分发送/解析/完成/取消/失败阶段；支持取消上传；401 触发重新
  登录；解析阶段单独提示，200–300MB 文件不再“卡住没反应”。
+ [优化] 代理策略与健康：账号级代理优先于账号组，账号组优先于全局默认组；空值/
  `global` 统一为继承；账号组不可用或节点全部熔断时回退全局默认，最终回退直连。
  组策略支持 `request_random` / `round_robin` / `time_window`；健康节点被剔除后跳过，
  冷却后恢复；主动探测与运行时使用同一健康键；图片出口按组节点配额限流，全部节点
  不可用时走直连；代理 URL 仅接受 http/https/socks5(h)，socks 自动升级 `socks5h`，
  拒绝内网/元数据地址与非法 scheme。
+ [优化] 旧库导入加固：上传前校验 SQLite 头、完整性、`accounts` 表与
  `access_token` 列，非 SQLite/损坏/空文件直接 400；流式读取超限中止并清理
  半成品临时文件；账号预览改为 SQL `LIMIT/OFFSET` 分页，关键字只解码候选行；
  自选导入按 `account_id` / `email` 精确匹配（大小写、空值、ID 回退），token 去重，
  已存在计为跳过；设置导入保护内部键与代理键，批写失败回退逐键写；账号组/设置/
  日志分开事务；调用日志 watermark 失败不推进、按导入文件隔离；`import_id` 拒绝
  路径穿越；大库元数据缓存，列表不再重复 `stat`。
+ [优化] 账号页性能 P0：表格视图接入窗口化渲染，只挂载视口附近行；`page_size`
  上限从 10000 收紧到 200；勾选改为按 key 追踪，单行切换 O(1)，不再重建全量选择集
  和整表 vnode；行组件按签名 `v-memo`，快照刷新时内容未变的行保留旧对象引用。
+ [优化] 图库 P1：卡片改为一次性视图模型 + 签名 `v-memo`，破图事件按微任务合批，
  避免单张 404 触发全部卡片重算；存储统计加 30 秒 TTL 与单飞，翻页/加载复用最近
  结果，不再每次追加全盘 `stat`。

### 测试

+ 新增 15 组回归测试，覆盖：文本切号重试、图片选号 500 映射、图片轮询时序、
  视觉/生图路由、账号组接口、安全 H-01/H-02/H-03、旧库导入（上传校验/预览分页/
  自选匹配/设置保护/日志 watermark/路径穿越）、代理策略与健康、调试日志保留与
  脱敏、账号治理确认与死号保护、协议兼容（Responses/Messages/工具调用）。
+ 新增用例同时锁住失败路径：空池/额度耗尽/未知选号错误、已输出 delta 后不重试、
  429 有界重试、代理健康键对齐、导入失败不推进 watermark。

### 说明

+ [说明] 本文件先于 `VERSION` 写入 1.1.9；父级更新 `VERSION` 前，
  `/api/system/changelog` 的 `current_version` 仍为 1.1.8，`test_changelog_endpoint`
  会报版本不一致，属预期过渡状态。
+ [说明] H-01 的最小长度按 12 位执行（生产建议 32 位随机串）；临时测试环境可用
  `QINGMENG_ALLOW_WEAK_AUTH_KEY=1` 显式放行，正式环境不要设置。
+ [说明] H-03 目前按 `Content-Length` 在解析前拒绝超过 2GB 的上传；无长度
  （chunked）请求仍依赖服务端流式上限，建议在反向代理层再配
  `client_max_body_size`。
+ [说明] 图片轮询参数仍可通过设置页调整；本版默认值只影响未显式配置的实例。

## [1.1.8] 2026-10-25 00:00

图片“挑选账号”提速（3 项优化）。

### 优化

+ [优化] 选号不再对每个候选都打一次 `/backend-api/me`：20 秒内刚校验成功、
  且状态/额度/机型都匹配的账号直接复用结果。以前一次请求最多串行 20 次远程校验
  （约 60 秒），现在常见情况 0 次。
+ [优化] 新增「前台优先槽」：图片选号的远程校验走优先通道，批量检测/刷新最多占用
  80% 的 `account_processing_concurrency`，给前台保留 20%，批量跑满时图片请求不再
  排在 512 个批量任务后面。
+ [优化] 图片轮询参数收紧：初始等待 5s → 3s，轮询间隔 5s → 3s，文件就绪后的 settle
  5s → 2s；单张图通常少等 4–6 秒。
+ [新增] 选号诊断日志：等待 ≥2s 时记录 `image_account_selected`
  （wait_ms、attempts、remote_checks、cache_hits），失败记录
  `image_account_selection_failed`，下次可直接定位是缓存未命中、远程校验慢还是池子没号。

### 测试

+ 新增：优先槽预留、批量不抢占、远程校验缓存命中/过期/失败、过期后走远程校验，
  共 **309 项全通过**。

## [1.1.7.3] 2026-10-24 00:00

容器启动自动修正挂载目录权限（小更新，1 项）。

### 修复

+ [修复] 宿主机上的 `data/` 由 root 创建时（宝塔/手动 mkdir 常见），容器内 UID 10001
  无法写库，表现为 `数据目录 /data 不可写` + `unable to open database file` 反复重启。
  现在入口脚本先以 root 检查并自动 `chown 10001:10001` + 补读写位，再用 `gosu`
  降权到 10001 运行应用；已有正确属主时跳过，不做无谓的全量 chown。
+ [修复] 自动迁移脚本也改为以 10001 身份执行，避免把旧库复制成 root 属主。
+ [变更] 容器不再以 `USER qingmeng` 直接启动，改为入口 root 修正权限后降权；
  应用进程仍然是 10001，安全边界不变。

### 部署

+ 全新机器直接 `docker compose up -d --build` 即可，不需要再手工 chown；
+ 已有 root 属主数据目录的机器，第一次启动会自动修正并打印
  `修正数据目录权限：/data -> 10001:10001`。

## [1.1.7.2] 2026-10-24 00:00

旧数据库“本地上传 + 自选导入”界面（小更新，1 项）。

### 新增

+ [新增] 设置页「旧项目目录同步」卡片内新增 **从数据库文件导入**：
  - 本地选择 `chatgpt2api.db` 上传并解析，显示账号 / 设置 / 账号组 / 日志数量；
  - 上传文件列表，可预览、删除（删除需二次点击确认）；
  - 账号预览表：邮箱/ID、状态、Token 摘要，支持搜索、分页、本页全选、清空；
  - 导入项可勾选：账号（全部 / 仅已选）、设置（只补缺 / 覆盖）、账号组、调用日志；
  - 先「预检（不写入）」看结果，再「确认导入」两次点击才写库；
  - 导入完成显示新增/跳过数量，并自动刷新文件列表与凭据统计。

### 说明

+ [说明] 设置默认「只补缺」，避免覆盖当前实例已经调好的配置；
+ [说明] 单文件上限 2GB，仅接受 SQLite 数据库，缺少 `accounts` 表会被拒绝。

## [1.1.7.1] 2026-10-24 00:00

调度保存与过载判定修复（小更新，2 项）。

### 修复

+ [修复] 调度策略保存时，未填写的字段不再被默认值覆盖：
  - `AdaptiveSchedulerPatch` 改为真正的部分更新，只提交的字段才生效；
  - 保存前校验「恢复阈值必须小于过载阈值」，不合法直接返回明确错误。
+ [修复] 设置页在切换「手动锁定 / 智能自动」或点「刷新」时会用服务端值覆盖正在编辑的
  阈值，导致刚填的 CPU 过载值跳回 85。现在：
  - 切换模式只同步模式，不再重置阈值；
  - 有未保存修改时刷新会保留输入，并提示「有未保存的修改」；
  - 新增「重新载入」按钮，明确放弃修改时才覆盖。
+ [修复] 过载判定不再被系统负载均值触发：`load1/cores` 只作诊断展示，
  降档只依据你设置的 CPU / 内存阈值，避免 CPU 60% 却显示过载。
+ [修复] Windows 下硬件采样与其他模块的 ctypes 定义冲突导致偶发 500（`GlobalMemoryStatusEx`），
  改为私有 kernel32 句柄。

### 测试

+ 新增：高负载均值不降档、策略部分更新保留其他字段、恢复阈值校验，共 **302 项全通过**。

## [1.1.7] 2026-10-24 00:00

> 版本号更正：此前误用了 1.2.0。按「1–2 项小更新记 1.1.6.N，3 项及以上记 1.1.7」的规则，
> 本次为多项功能更新，正确版本号为 **1.1.7**。后续小更新从 1.1.7.1 起。

试运营就绪版本：8K 图片、500+ 并发准入控制、调度算法增强。

试运营就绪版本：8K 图片、500+ 并发准入控制、调度算法增强。

### 新增

+ [新增] 图片尺寸新增 **8K 预设**：`7680x4320`、`4320x7680`、`8192x8192`（后端放大上限 8192）。
+ [新增] 全局请求准入闸门 `services/admission.py`：
  - 上限按硬件推导（32 核 ≈ 512，64 核 ≈ 1024，128 核 ≈ 2048，环境变量
    `QINGMENG_MAX_REQUEST_CONCURRENCY` 可覆盖）；
  - 超限请求**排队等待**，等待超时（对话 60s / 图片 180s）返回 **503 + Retry-After**，
    不会把线程池和内存撑爆；
  - 在途 / 排队 / 拒绝 / 等待分位统计可观测。
+ [新增] `GET /api/system/load`：准入统计 + 线程池令牌 + 调度器快照 + 队列状态。
+ [新增] `/metrics` 新增 `qingmeng_inflight_requests`、`qingmeng_queued_requests`、
  `qingmeng_request_limit`、`qingmeng_requests_rejected_total`。
+ [新增] 测试：准入闸门 6 组（并发上限、排队唤醒、超时拒绝、环境变量、单例绑定）、
  8K 相关接口（共 298 项）。

### 优化

+ [优化] 线程池令牌改为跟随准入预算（32 核 512），不再受旧的 128 限制；
  准入与线程池由同一个调度器缩放，避免「请求进来了但没有线程」。
+ [优化] 调度器新增**每核负载**信号：`load1/cores ≥ 1.5` 视为过载、
  `≥ 1.0` 不判定健康，CPU 采样窗口内的瞬时排队也能被提前识别。
+ [优化] `THREAD_TOKEN_FLOOR` 32 → 64，低负载时也不至于过窄。

### 说明

+ [说明] 500 并发是「准入 + 排队 + 超时」的工程上限：是否全部立刻处理取决于账户/上游
  吞吐，但服务端会稳定接收、有序调度，不会因突发流量卡死容器。

## [1.1.6] 2026-10-24 00:00

可开关的调试日志：按时间或体积保留，支持一键导出脱敏调试包。

### 新增

+ [新增] `services/debug_log_service.py`：默认关闭；开启后把完整应用日志写入
  `data/debug-logs/debug-*.log`（JSON Lines，单文件 64MB 滚动）。
+ [新增] 两种保留方式：
  - **按时间**：保留最近 N 天（默认 30 天），更早文件自动删除；
  - **按体积**：总量目标 `max_size_mb`（默认 1024MB），允许上浮 `slack_mb`（默认 100MB），
    超过「目标 + 上浮」时从最旧文件开始删，稳定在「目标 ~ 目标+上浮」区间。
+ [新增] 导出调试包：`POST /api/system/debug-logs/export` 返回 zip（最近日志 + `system-info.json`
  + 说明），单包最多 300MB；日志与系统信息自动脱敏（JWT、Bearer、sk- 密钥、token/password 字段）。
+ [新增] 接口：`GET/POST /api/system/debug-logs`、`/export`、`/clear`。
+ [新增] 设置页「调试日志」卡片：启用开关、保留方式、天数/体积/上浮、采集级别、
  下载调试包、清空、当前文件数与占用。
+ [新增] 测试：时间/体积保留、上浮区间、脱敏、开关生效、导出结构、接口（共 276 项）。

### 说明

+ [说明] 调试日志开启时会临时忽略 `log_levels` 的过滤，保证 debug 级别也能落盘；
  关闭后立即恢复原行为。
+ [说明] 运行日志（`call_records`）与调试文件日志相互独立，互不影响。

## [1.1.5] 2026-10-24 00:00

修复 Codex 等客户端带 `reasoning.effort` 时上游 422。

### 修复

+ [修复] 上游部分账号/模型组合不接受 `thinking_effort` 字段（返回 422），
  导致 Codex CLI 的 `/v1/responses` 请求一律 502。现在检测到 422 且请求体带
  `thinking_effort` 时，去掉该字段自动重试一次，其余行为不变。

### 说明

+ [说明] 不影响已支持推理档位的账号；只在被上游拒绝时降级，不改变默认请求。

### 实测（线上 1.1.5，free 网页号 + 美国代理组）

+ `/v1/models` → `["gpt-6","auto","gpt-image-2","gpt-image-2.5","gpt-image-2.5-flare","gpt-image-2.5-sunburst"]`。
+ 连续对话（gpt-6）：两轮，第二轮准确答出第一轮数字 `7391`。
+ Codex 风格 `/v1/responses`（带 `instructions`/`tools`/`reasoning`/`store`/`include`/`prompt_cache_key` 等）：
  200，输出 `function_call`（`shell`，`call_id` 正常）。
+ Codex 风格流式：`response.created` → `response.output_item.added` →
  `response.function_call_arguments.delta` → `response.output_item.done` → `response.completed`。



## [1.1.4] 2026-10-24 00:00

对外模型名统一为 `gpt-6`。

### 变更

+ [变更] 对外模型名（`/v1/models`、响应体 `model` 字段、控制台模型下拉）统一为 **`gpt-6`**；
  默认对话模型改为 `gpt-6`；`auto` 保留。
+ [变更] 上游仍使用其认识的 slug：`gpt-6` → `gpt-5.6`（`gpt-6-latest` / `gpt-6-mini` 同映射）；
  旧的 `gpt-5.6` 仍可使用，对外展示统一折叠为 `gpt-6`。
+ [新增] 模型别名模块 `services/protocol/model_aliases.py`（public ↔ upstream 双向映射）。
+ [优化] 绘画模型提示文案同步改为「对话请使用 gpt-6 / auto」。

### 说明

+ 绘图模型名不变（`gpt-image-2`、`gpt-image-2.5` 等），端点与模型校验规则不变。

## [1.1.3] 2026-10-24 00:00

`/v1/responses` 工具调用桥接：Responses 协议的 Agent 也能用 free 网页号调本地工具。

### 新增

+ [新增] Responses 端点工具桥接：`tools`（扁平 function 格式）自动转成桥接层结构，
  复用同一套 JSON 信封、纠正重试与安全约束。
+ [新增] 历史折叠：`function_call` / `function_call_output` 项自动转成 assistant tool_calls
  与 `role:"tool"` 消息，再折成上游可读文本。
+ [新增] 响应输出：`function_call` 项（`call_id` / `name` / `arguments` / `status`），
  非流式补 `output_text` 便捷字段；流式输出
  `response.output_item.added` → `response.function_call_arguments.delta` → `response.output_item.done` → `response.completed`。
+ [新增] 测试：Responses 桥接 5 组（共 264 项）。

### 说明

+ [说明] 同时带 function 与 web_search 工具时优先走工具桥接；图片生成工具仍走原图片链路。

### 实测（线上 1.1.3，free 网页号 + 美国代理组）

+ 普通 responses：200，`output_text="OK"`。
+ 工具调用：200、`status=completed`，输出项
  `{"type":"function_call","call_id":"call_...","name":"shell","arguments":"{\"command\": \"pwd\"}"}`。
+ 回传 `function_call_output`：200、`status=completed`，`output_text="`pwd` returned: /opt/qingmeng"`。
+ 流式：事件顺序 `response.created` → `response.output_item.added` →
  `response.function_call_arguments.delta` → `response.output_item.done` → `response.completed`。


+ [说明] 工具契约同样注入最后一条用户消息（网页后端不执行 system 指令）。

## [1.1.2] 2026-10-24 00:00

Anthropic `/v1/messages` 工具桥接：Claude Code 等 Anthropic 协议客户端也能用 free 网页号调工具。

### 优化

+ [优化] 用桥接仓库原版 `anthropic_v1_messages.py` 替换本仓库旧实现：JSON 信封（与 chat 端点同一套
  `tool_system_prompt` / `parse_pairs`），不再使用自造的 XML 规则。
+ [优化] Claude Code 的 system prompt 自动压缩（保留关键契约，避免撑爆上下文）；
  `tool_use` / `tool_result` 历史折叠成上游可读文本。
+ [优化] 响应转换为 Anthropic 内容块：`tool_use` + `stop_reason=tool_use`；流式按内容块增量下发，
  遇到 JSON 信封起点即停止文本流（`streamable_prefix`），避免半截信封泄漏。
+ [优化] 空回复/坏信封与非工具轮重试复用 chat 端点同一套逻辑。

### 实测

+ 单测：**259 passed**（含原版 129 组桥接测试，Anthropic 用例已全部启用）。
+

### 实测（线上 1.1.2，free 网页号 + 美国代理组）

+ 普通 messages：200，`content=[{"type":"text","text":"OK"}]`。
+ 工具调用：200、`stop_reason=tool_use`，内容块
  `{"type":"tool_use","id":"toolu_...","name":"shell","input":{"command":"pwd"}}`。
+ 工具结果回传：`tool_result` → 200、`stop_reason=end_turn`，回答「`pwd` returned: /opt/qingmeng」。


## [1.1.1] 2026-10-24 00:00

工具调用桥接升级为 chatgpt-web-tool-bridge 原版协议实现，替换 1.1.0 的简化版。

### 优化

+ [优化] 引入上游作者的 `openai_tool_bridge.py`（42KB）与 `tool_markup.py`：
  `AVAILABLE_TOOLS` 提示词、工具 schema 自动压缩（>8 个或 >4000 字符）、
  XML 历史序列化、多级解析兜底（JSON 信封 / `<tool_calls>` / 沙箱回声）。
+ [优化] 三条安全约束：未知工具名丢弃、schema 外参数剥离、破坏性整文件重写调用拒绝。
+ [优化] 工具轮「缓冲整轮 + 带原因纠正重试（最多 3 次）」；有可用调用时正文恒为空；
  全部失败返回 503 + `Retry-After`（空回复/坏信封/不可达分类）。
+ [优化] 中文拒答识别、截断 XML 标记尾巴剥离、计划式回答/未验证检索识别的纠正话术。
+ [优化] 传输层失败识别（curl 28/7/35/52/56/60、TLS、连接重置）与换号重试。
+ [优化] 非工具轮也带空回复重试，避免客户端拿到 `finish_reason=stop` 但内容为空。
+ [优化] 保留本仓库适配：工具契约注入**最后一条用户消息**（网页后端不执行 system 指令），
  并对长对话做条数/字符双预算压缩。
+ [新增] 移植上游测试集 `tests/test_openai_tool_bridge.py`（129 组），全量 **253 passed**；
  Anthropic 端点桥接未移植，6 组用例跳过。

### 说明

+ [说明] `tool_turn` 等实现直接使用模块级 `stream_text_deltas`，与上游测试的 monkeypatch 兼容。

### 实测（线上 1.1.1，free 网页号 + 美国代理组）

+ 非工具对话：200，返回 “OK”。
+ 工具调用：`tools=[shell]` + “run pwd” → `finish_reason=tool_calls`、`shell({"command":"pwd"})`、正文为空。
+ 工具结果回传：`role:"tool"` → 200、`stop`、回答「client's current working directory is `/opt/qingmeng`」。
+ 写文件：`tool_choice=required` → 模型选择 `shell({"command":"printf 'hello bridge' > report.txt"})`（README 已记录的已知倾向，仍为可用调用）。
+ 流式工具轮：SSE 依次给出 `tool_calls[0].function.name=shell` → 参数分片 `{"command":"whoami"}` → `finish_reason=tool_calls`。

+ [说明] 原 `services/protocol/tool_bridge.py` 已删除，统一由 `openai_tool_bridge.py` 承担。

## [1.1.0] 2026-10-24 00:00

协议层大版本：端点-模型强校验、连续对话历史治理、网页 free 账号的工具调用桥接。

### 新增

+ [新增] **工具调用桥接**（`services/protocol/tool_bridge.py`）：把 OpenAI `tools` 渲染成
  严格 JSON 信封提示词，解析模型输出为标​​准 `tool_calls`；`role:"tool"` 与历史 `tool_calls`
  改写成模型可读的纯文本，解决网页后端收到 tool 角色直接 500 的问题。
  - 信封：`{"tool_calls":[{"name":"..." ,"arguments":{...}}]}`
  - 容忍代码块、前后寒暄、参数为字符串、单条 `{"name","arguments"}`、`{"tool_call":{...}}`
  - `tool_choice=required` / 指定函数会强化提示
  - 流式请求先缓冲整轮再判定（信封可能跨多个 chunk），命中则推送 `tool_calls` 增量
+ [新增] 端点-模型强校验：绘画模型调用 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`
  返回 400 `model_endpoint_mismatch`；文本模型调用 `/v1/images/*` 返回 400 并列出受支持的绘画模型。
+ [新增] 连续对话历史治理（`services/protocol/history.py`）：统一把 tool/function 角色归一为文本，
  并按「条数 + 字符」双预算压缩历史（保留全部 system 与最近轮次），避免长对话请求体无限膨胀。
+ [新增] 测试：工具桥接 9 组、历史压缩 3 组、模型路由 2 组（共 139 项）。

### 优化

+ [优化] 连续对话：此前 tool 角色历史会被原样发给上游（直接 500），现在任何请求都会先归一化角色。

### 实测（线上 1.1.0，free 网页账号 + 美国代理组）

+ 端点校验：`chat + gpt-image-2` → 400 `model_endpoint_mismatch`；`images + gpt-5.6` → 400 并列出受支持绘画模型。
+ 连续对话：system + 2 轮，第二轮准确回忆第一轮数字（4682）；流式第三轮计算正确。
+ 工具调用：`tools=[shell]` 请求 → `finish_reason=tool_calls`、`shell({"cmd":"pwd"})`；
  回传 `role:"tool"` 结果后 → `finish_reason=stop`、回答「当前目录 /opt/qingmeng」，上游未 500。
+ 说明：网页后端不执行 `role:"system"` 里的工具指令，工具契约改为注入**最后一条用户消息**后生效。


### 说明

+ [说明] 工具桥接只作用于 `/v1/chat/completions`（含流式）；`/v1/responses`、`/v1/messages`
  仍走文本路径，后续版本再扩展。
+ [说明] 桥接不执行任何工具，只做协议翻译；真正的工具执行仍由本地 Agent 完成。

## [1.0.11] 2026-10-24 00:00

对话/绘画链路优化与排障：上游连通性自检、超时错误提示、单行保存。

### 新增

+ [新增] `GET /api/system/upstream-diagnostics`：检测 `chatgpt.com` / `auth.openai.com`
  的 DNS（IPv4/IPv6 分布）、直连 TCP+TLS 耗时、当前默认代理，并给出可执行建议。
+ [新增] 设置页「凭据体检」卡片增加「上游连通性」按钮，直接显示结论。
+ [新增] 测试：单行保存、连通性自检结构（共 124 项）。

### 优化

+ [优化] 对话请求的 `mark_text_used` 从「全表 diff + 单行写」改为**只写当前账号这一行**
  （O(1)），高并发对话时不再每次请求都扫描整个号池。
+ [优化] 上游连接超时（curl 28 / connection timed out）的错误信息追加中文提示：
  优先检查该账号是否走可用代理、容器内 `chatgpt.com` 是否被解析到不可用 IPv6。

### 实测

+ 配置代理默认出口后：`/v1/chat/completions` 200（约 9s，返回 “OK”）；
  `/v1/images/generations` 200（约 53s，返回 b64 图片）。
+ 未配置代理时两者均超时——根因是容器内 `chatgpt.com` 只解析到 IPv6 且直连被 Cloudflare 拦截。

## [1.0.10] 2026-10-24 00:00

路线图 P1：本地自动备份 + 管理员操作审计。

### 新增

+ [新增] `services/local_backup_service.py`：每日用 SQLite backup API 生成一致快照到
  `data/backups`，按保留份数自动轮换；PostgreSQL 部署自动跳过并提示改用 R2/pg_dump。
+ [新增] 接口 `GET/POST /api/system/local-backup`、`POST /api/system/local-backup/run`。
+ [新增] `services/audit_service.py`：管理员关键操作写 `data/audit.jsonl`（JSONL，2MB 轮转），
  只记录动作、对象数量与摘要，不记录 token/密钥。
+ [新增] 已接入审计：账号批量更新、账号删除、死号清理、治理执行、备份配置/执行。
+ [新增] 接口 `GET /api/system/audit?limit=&action=`。
+ [新增] 设置页新增两张卡片：「本地自动备份」（启用/间隔/保留份数/立即备份/最近备份列表）、
  「操作审计」（最近 30 条，含时间、动作、对象）。
+ [新增] 测试：备份快照与轮换、PostgreSQL 跳过、审计写入/过滤、接口 4 组（共 121 项）。

## [1.0.9] 2026-10-24 00:00

路线图 P0 收尾 + P2 观测：死号自动治理、账号日志独立保留、Prometheus 指标。

### 新增

+ [新增] `services/account_governance_service.py`：按间隔处理「AT 失效且 RT 失效」的死号，
  三档策略 `report / disable / purge`；默认只报告、默认关闭；单次上限 `max_per_run` 保护，
  超限自动降级为只报告。
+ [新增] **账号日志独立保留期**：`account` 类型日志单独按 `account_log_retention_hours`
  （默认 168h）清理，不再和业务调用日志共用 720h；清理走 `cleanup_before(record_type=...)`，
  只删指定类型。
+ [新增] `GET/POST /api/system/account-governance`、`POST /api/system/account-governance/run`。
+ [新增] `GET /metrics`（Prometheus 文本格式，管理员凭证鉴权）：账号分桶、可续期数/占比、
  队列任务与令牌数、数据库体积。
+ [新增] 设置页「凭据体检」卡片增加「自动治理与日志瘦身」：模式、间隔、日志保留期、保存与预检。
+ [新增] 测试：治理服务 6 组、接口 2 组（共 116 项）。

### 说明

+ [说明] 账号治理只针对 `unavailable`（AT 失效且 RT 失效/缺失）；`usable`（AT 仍有效）
  与 `recoverable`（AT 失效但 RT 可用）不会被处理。

## [1.0.8] 2026-10-24 00:00

按优化路线图落地 P0 前两项：账号页凭据筛选、号池健康告警。

### 新增

+ [新增] 账号列表支持按**凭据状态**筛选：可用 / 可恢复 / 死号 / RT 缺失 / RT 失效；
  筛选条件会带入全选作用域（`AccountSelectionScope.credential`），筛选后全选只会命中筛选结果。
+ [新增] `services/pool_alert_service.py`：定时统计可用账号数与可续期占比，低于阈值时写日志并推送 Webhook，
  同一问题在冷却期内只报一次；配置与状态落在 `data/pool_alerts.json` 与 `.pool-alerts-state.json`。
+ [新增] 接口 `GET/POST /api/system/pool-alerts`、`POST /api/system/pool-alerts/run`（支持 dry-run）。
+ [新增] 设置页「凭据体检」卡片增加「低可用告警」配置：启用开关、Webhook URL、最低可用账号、最低可续期占比、立即体检。
+ [新增] 测试：凭据筛选 4 组、告警服务 6 组、接口 3 组（共 108 项）。

### 说明

+ [说明] 告警默认关闭；未填 Webhook 时只在界面与日志中提示，不会外发任何请求。

## [1.0.7] 2026-10-24 00:00

把「RT 失效的账号还能不能用」变成可查、可清理：新增凭据体检与死号清理。

### 新增

+ [新增] `GET /api/accounts/credential-summary`：按 AT×RT 六种组合分桶统计——
  `usable`（AT 有效，还能请求）、`recoverable`（AT 失效但 RT 可用，下轮刷新恢复）、
  `unavailable`（AT 失效且 RT 失效/缺失，彻底不可用），并返回各桶数量与死号数量。
+ [新增] `POST /api/accounts/credential-cleanup`：预检（`dry_run=true`，默认）与实际删除
  `unavailable` 账号，删除走既有批量删除通道（会写账号日志）。
+ [新增] 设置页「凭据体检（AT / RT）」卡片：三桶数字、AT/RT 明细、两步确认清理死号。
+ [新增] API 冒烟测试覆盖体检与预检接口（共 97 项）。

### 说明

+ [说明] RT 失效不等于 AT 失效：AT 是独立 JWT，未过期且未撤销就仍可请求；
  只有「AT 过期 + RT 失效」才是彻底不可用。选号层已按此判定，不会把可请求的账号排除。

## [1.0.6] 2026-10-24 00:00

新增「旧项目目录同步」：给一个旧项目的持久化目录路径，按间隔增量同步账号、功能设置、
账号组与日志，并把新账号自动送进测活队列。

### 新增

+ [新增] `services/legacy_sync_service.py`：只读扫描旧库，按 `storage_revisions` 判断
  账号是否变化，未变化直接跳过；账号只增不改不删，设置默认只补缺（可切覆盖）。
+ [新增] 新账号自动测活：同步到的账号按每轮上限投递到既有检测队列，不额外起线程池。
+ [新增] 接口 `GET/POST /api/system/legacy-sync`、`POST /api/system/legacy-sync/run`：
  查看/保存配置、探测路径可用性、立即同步或试运行（dry-run 不写库）。
+ [新增] 设置页「旧项目目录同步」卡片：目录多行输入、同步间隔、设置同步方式、
  账号/账号组/日志/自动测活开关、每轮上限、路径探测结果与最近同步摘要。
+ [新增] 测试：同步服务 5 组用例 + 接口 2 组用例（共 96 项）。

### 说明

+ [说明] 旧库全程只读打开；同步状态写入 `data/legacy_sync.json` 与
  `data/.legacy-sync-state.json`，不新增数据库表，旧库版本差异不影响使用。
+ [说明] 代理节点不自动覆盖（避免打断线上代理），需要时在代理页导入。

## [1.0.5] 2026-10-24 00:00

账号刷新日志风暴修复：失效 refresh_token 被无限重试，导致数据库每天多出约百万条账号日志。

### 修复

+ [修复] `refresh_token_reused` / `refresh_token_expired` / `refresh_token_revoked` /
  `invalid_token` / `token_expired` 未被判定为**终态**刷新失败，账号永远停留在「待刷新」，
  生命周期巡检每 5 分钟重试一次并写一条日志。迁移后实测 2 小时产生 **72,960 条**账号日志
  （约 100 万条/天），数据库快速膨胀。现归入终态并写入 `refresh_token_invalid_at`，
  同一个账号只失败一次即退出重试。
+ [修复] 非终态刷新失败（网络抖动、429、5xx）没有冷却，同一账号每个巡检周期都会重试。
  新增 5 分钟退避（读取 `last_token_refresh_error_at`），过期扫描跳过冷却中的账号。
+ [新增] `tests/test_refresh_backoff.py`：终态错误判定、退避跳过、已失效 RT 跳过共 3 组用例。

### 说明

+ [说明] 本版本只改业务判定与重试策略，**数据库表结构没有任何变化**，旧库可直接使用。

## [1.0.4] 2026-10-24 00:00

控制台补上「更新日志」能力：版本信息与变更记录在界面内可查，且完全本地读取、不联网校验。

### 新增

+ [新增] 侧边栏底部显示当前版本号，点击打开「更新日志」弹窗。
+ [新增] 更新日志查看器：按版本展示日期与分组（新增 / 优化 / 修复 / 移除 / 重构），
  当前版本高亮，可切换「只看当前版本线 / 查看历史版本」。
+ [新增] `GET /api/system/changelog`：解析项目自带 `CHANGELOG.md`，按内容哈希缓存，
  只读本地文件、无任何外部请求。
+ [新增] `tests/test_changelog_service.py`：版本解析、归档分隔、条数上限、文件缺失、
  内容变更后缓存失效共 5 组用例。

### 优化

+ [优化] 更新日志版本规则明确为：小功能/优化/修复递增小版本号，大更新递增大版本号。

### 说明

+ [说明] 本版本替代了旧版被删除的「版本更新弹窗」：只展示本地更新日志，不再检查远端版本。

## [1.0.3] 2026-10-24 00:00

账号规模上来后「打开账号页慢、全选操作卡死」的性能专项。测试机 7,330 账号实测：
进度轮询单次响应 16.3 MB、全选批量操作耗时 178 秒，本版本分别降到 KB 级与数秒级。

### 修复

+ [修复] 批量操作进度轮询会重新投影**全部**账号行（`items` 未关闭）并回传全部 ID：
  7,330 账号时单次轮询 **16.3 MB**，浏览器与事件循环同时被拖死。现固定 `include_items=False`，
  ID 只回传前 500 个并附带 `updated_total / removed_total / ids_truncated`。
+ [修复] 大批量操作逐账号写进度事件（每个账号一次加锁 + 事件脱敏）：改为前 200 条抽样事件 +
  一次计数推进（`advance_refresh_progress`），其余合并为一条汇总事件。
+ [修复] 批量更新结束后重建**全表**管理 ID 集合：改为按令牌 O(1) 判断存活。
+ [修复] `access_token` JWT 载荷在每次行投影、就绪判定、选号、事件里重复解码：
  改为进程内 LRU 缓存（8,192 条），同一令牌一次请求只解码一次。
+ [修复] 账号候选索引分类对每个账号做两次凭据可用性判定（两次解码）：合并为一次。
+ [修复] 账号组统计每次请求遍历全池：改为账号存储增量维护 + 脏标记缓存，并对 `/api/account-groups` 的投影做 2 秒缓存（分组变更即时失效）。
+ [修复] 大号池每隔 5 秒整表重载造成周期性卡顿（7k 账号单次 0.5s）：
  快照 TTL 按规模分档（>2k 15s、>1万 30s、>5万 60s、>25万 120s）。

### 优化

+ [优化] 账号列表 20 条 / 500 条的投影耗时随 JWT 缓存与分组缓存下降。
+ [优化] 大批量操作的进度抽屉最多渲染 200 条事件 + 1 条汇总，避免上千 DOM 节点。

### 实测（测试机 16C/31G，7,330 账号，同一份生产库快照）

| 场景 | 1.0.2 | 1.0.3 | 变化 |
| --- | --- | --- | --- |
| 账号列表 page_size=20 | p50 83ms / max 559ms | **p50 18.9ms / max 98ms** | 4.4× |
| 账号列表 page_size=100 | p50 157ms | **p50 84.9ms** | 1.8× |
| 账号列表 page_size=500 | p50 571ms | **p50 301ms** | 1.9× |
| 账号组统计 | p50 171ms / max 70s | **p50 90ms / max 3.7s** | 大幅收敛 |
| 全选预览 | p50 121ms / max 79s | **p50 27.7ms / max 50ms** | 4.4× |
| 全选批量操作提交 | 79.4s | **0.65s** | 122× |
| 全选批量操作总耗时 | 178s | **2.8s** | 63× |
| 进度轮询单次响应 | 16.3 MB | **128 KB** | 127× |

补充修复：

+ [修复] 周期性全量快照重载改造为「先查 revision，变了才重载」（`StorageBackend.load_accounts_revision`）：
  单进程部署下 7,330 账号每 15 秒一次的全表加载+规范化+回写被彻底消除。
+ [修复] 每次账号保存后对整个号池做 `deepcopy`：改为只同步实际写入的行，
  单账号保存从 O(pool) 降到 O(1)，后台刷新不再长时间持有账号锁。
+ [修复] 全选/预览用 `list_accounts()`（复制全部账号字典）构造目标：改为直接读实时记录，
  7,330 账号的全选预览从 1.8s 降到 28ms。
+ [新增] `tests/test_snapshot_refresh.py`：revision 未变不重载、revision 变化触发重载。

## [1.0.2] 2026-10-24 00:00

启动即自动接管旧数据：容器第一次启动时会自己发现旧库并完成迁移，不再需要手工执行脚本。

### 新增

+ [新增] `scripts/auto_migrate_legacy.py`：容器入口在 uvicorn 启动前自动执行。
  自动探测 `/legacy`、`/legacy-data`、`/old-data`、`/app/data`、`/data/legacy` 与
  `QINGMENG_LEGACY_DIRS` 指定的目录，发现含账号的旧库即开始迁移。
+ [新增] 多源合并：以账号最多的旧库为基线快照，其余旧库的账号 / 授权密钥 / 调用日志按唯一键
  `INSERT OR IGNORE` 合并（同一 AT 不会重复入库），设置、代理组、账号组只在目标为空时继承。
+ [新增] 迁移安全阀：目标库已有账号时直接跳过并打印原因，绝不覆盖；旧库全程只读打开；
  成功后写 `.auto-migrate.json` 标记（来源、账号数、耗时、保留期、最近 10 次运行），后续启动自动跳过。
+ [新增] `--merge` 增量合并模式：目标库已有账号时也能把其它旧库并入（只 INSERT OR IGNORE，
  不覆盖现有账号与设置），用于把多套旧部署的账号汇到一个新实例。
+ [新增] `QINGMENG_AUTO_MIGRATE`（1/0）与 `QINGMENG_LEGACY_DIRS`（多个用 `:` 分隔）两个开关，
  compose 已内置示例挂载 `- /旧数据目录:/legacy:ro`。
+ [新增] `tests/test_auto_migrate_legacy.py`：多源合并去重、二次启动幂等、开关关闭、无源目录共 4 组用例。

### 优化

+ [优化] 迁移完成自动冻结日志保留期（旧库未显式设置时写入 8760 小时），避免首次启动清理历史。
+ [优化] 数据文件（图片/缩略图/任务/可编辑文件）按内容与大小跳过已存在文件，重复迁移不重复复制。

## [1.0.1] 2026-10-24 00:00

真机（测试服务器 16C/31G）部署验收时发现的问题修复，并新增旧版数据迁移能力。

### 新增

+ [新增] `scripts/migrate_legacy_data.py`：旧版 chatgpt2api → 倾梦GPT 数据迁移工具。
  支持 SQLite 直接快照迁移、PostgreSQL 走新版 StorageBackend 写入、`--dry-run` 预演、
  `--verify` 启动级校验（账号是否能全部加载、调度索引覆盖多少）、图片与任务文件一并复制。
+ [新增] 迁移默认把日志保留期冻结为 8760 小时：首次启动的保留清理不会删除旧库历史日志
  （实测旧库 28,929 条调用日志，若按默认 24h 保留会在启动瞬间被清空，已在 1.0.1 中规避）。
+ [新增] `tests/test_migrate_legacy_data.py`：迁移工具的预演、正式迁移、文件复制、强制覆盖备份、
  历史保留策略共 3 组用例。

### 修复

+ [修复] 批量导入复杂度 O(n²)：每导入一个账号都会重建一次全池管理 ID 集合，20 万账号十分钟
  跑不完。改为批次内共享集合后，测试机实测 **20 万账号 73.5s 完成（约 2,720 账号/秒）**。
+ [修复] 深度翻页 O(offset)：20 万账号第 1900 页需 102ms。无墓碑时直接切片后 **0.10ms**。
+ [修复] 前端 history 路由硬刷新返回 404（`/accounts`、`/settings` 等）。非 API 路径回退
  index.html，未知 API 路径仍返回 404。
+ [修复] 账号列表投影阻塞事件循环，改入线程池执行。

### 优化

+ [优化] 迁移工具输出源库账号/日志/代理/设置清单，目标已存在时强制备份并禁止静默覆盖。

## [1.0.0] 2026-10-23 00:00

首个企业级重构版本。底层调度、队列、数据库、并发与前端主题全部按 100 万账号标准重做，
旧版 7000+ 账号下出现的「选号 2 分钟、检测不准、AT 刷新失效、代理不切换、高并发崩机、配置死板」
逐项定位并修复。

### 重构

+ [重构] 新增 `services/account_index.py`：选号改为增量维护的候选索引。按「模式 / 套餐 / 来源」分桶，
  每桶用环形队列保存仍有余量的令牌，选号从「每次请求全表扫描」变为常数时间出队；
  账号重新分类只换 epoch 惰性失效，不产生 O(n) 删除；忙号到达并发上限即退出队列，释放后自动回队。
+ [重构] `services/account_service.py` 接入调度索引：账号表改为带写入钩子的映射，新增 / 更新 / 删除 /
  令牌轮换同步维护索引，图片选号、文本取号、在途计数全部走索引路径。
+ [重构] 账号表新增管理 ID 反向索引与插入顺序表：按 ID 查账号、ID 批量解析不再全表遍历；
  分页改为「无筛选直接切插入序窗口，有筛选流式扫描并只投影当前页」。
+ [重构] 快照刷新 TTL 随号池规模自适应（1 万 / 5 万 / 25 万以上分别放宽），
  百万号池不会再出现「整表重载一直在跑」。
+ [重构] 新增 `services/account_check_queue.py`：批量检测、AT 刷新、号池同步统一进入异步队列。
  单一有界线程池 + 检测/刷新/同步三条通道轮转取任务 + 一次只领一个令牌，
  百万账号任务只占「一个指针/账号」，不再为每个账号创建一个 future。
+ [重构] 新增 `services/hardware_profile.py`：启动即识别 CPU 核心数、CPU 型号、内存、磁盘容量与 IO，
  自动判定低配 / 中配 / 高配 / 旗舰并给出推荐基础参数（仅作默认值，不覆盖用户保存值）。
+ [重构] 新增 `services/adaptive_governor.py`：智能自适应负载调度。CPU > 85% 或内存 > 90% 判定过载并
  立即逐级降配；CPU < 70% 且内存 < 80% 连续 300 秒后一次性精准回弹到用户原始设置。
  支持「智能自动 / 手动锁定」双模式，全程不回写用户配置。
+ [重构] 全局账号处理并发上限 100 彻底解除：`account_processing_concurrency` 改为无上限（最小 1），
  实际生效值 = 用户保存值 × 当前调度比例（下限 1）。
+ [重构] 异步线程池大小改为网页配置，并由调度器联动伸缩（过载降、恢复升），不再依赖环境变量。
+ [重构] 新增 `services/proxy_health.py`：出口健康注册表。被动记录请求成败 + 主动探测代理组节点，
  连续失败熔断、冷却后自动恢复、失败越多冷却越久；整组不可用时自动回退直连。
+ [重构] 新增 `services/database_maintenance.py`：数据库健康检查、统计信息优化、索引重建、空间压缩、
  WAL 截断与一键修复，SQLite 与 PostgreSQL 分别适配。
+ [重构] 数据库概览新增碎片率、空闲页、WAL 体积等指标，维护步骤逐条返回耗时与结果。
+ [重构] 设置页新增「智能调度与维护」分组：硬件识别、实时负载、生效比例、阈值策略、维护按钮、
  队列状态、代理健康、主题切换集中在一页完成，全部配置网页可视化，无需改 yml。
+ [重构] 概览中心新增「1 小时」统计周期，与 24 小时 / 7 天 / 30 天并列；前后端契约同步。
+ [重构] 前端主题系统重做：默认 / 倾梦·青瓷 / 星夜·霓虹 / 樱语·柔光 / 曜石·极简五套独立皮肤，
  配色、圆角尺度、字体搭配、密度、描边与投影各自独立，右上角一键切换。
+ [重构] 品牌私有化：控制台名称、登录页、页面标题、侧边栏、浏览器存储键、搜索技能、WebDAV 默认目录、
  Python 包名与镜像名统一为倾梦 / qingmeng 体系；环境变量新增 `QINGMENG_*` 并保留旧名兼容。

### 新增

+ [新增] `POST /api/accounts/check`：批量账号检测，走异步队列并复用统一进度投影。
+ [新增] `GET /api/system/scheduler`、`POST /api/system/scheduler/mode`、`POST /api/system/scheduler/policy`：
  硬件与负载状态、双模式切换、阈值策略保存。
+ [新增] `GET /api/system/database`、`POST /api/system/database/maintenance`：
  数据库状态与一键修复 / 优化 / 重建索引 / 压缩。
+ [新增] `GET /api/system/queue`、`POST /api/system/queue/{job_id}/cancel`：任务队列观测与取消。
+ [新增] `GET /api/system/proxy-health`、`POST /api/system/proxy-health/probe`：出口健康与立即探测。
+ [新增] 账号列表分页快速选择 500 / 1000 / 5000 条，并提供「自定义每页数量」输入（上限 10000）。
+ [新增] `tests/` 测试套件：调度索引、异步队列、自适应调度器、数据库维护、账号选择集成共 66 项通过。
+ [新增] `scripts/bench_account_selection.py`：百万账号调度基准脚本，可复现选号延迟数据。
+ [新增] Docker 部署：`Dockerfile`、`docker-compose.qingmeng.yml`、`deploy/docker-entrypoint.sh`，
  处理 DNS 解析异常与容器网络兼容，镜像名自带 `qingmeng`。
+ [新增] 文档：架构现状分析、百万级目标架构、自适应调度说明、Docker 部署运行手册。

### 优化

+ [优化] 账号分组统计与操作进度投影不再复制全量账号，改为流式读取，内存占用与单次请求耗时同步下降。
+ [优化] 无筛选翻页只读取当前页窗口，不再为整池账号创建字典副本；无墓碑时直接切片，
  20 万账号深度翻页从 102ms 降到 0.10ms。
+ [优化] 进度错误脱敏只使用当前操作自带的令牌提示，去掉全池扫描。
+ [优化] 调度索引仅在状态、额度、凭据、套餐、来源变化时重新分类，普通计数与时间戳更新不触发。
+ [优化] 前端图标集按实际使用重新生成，移除已删除功能的残留图标。
+ [优化] 账号列表投影移入线程池，整池规模的分页计算不再阻塞事件循环。

### 修复

+ [修复] 选号耗时随账号数线性增长（7000 账号已需数十秒到 2 分钟）。改为索引出队后 p50 ≈ 0.003ms，
  百万账号实测 24.7 万次选号/秒。
+ [修复] 批量导入复杂度为 O(n²)：每导入一个账号都会重建一次全池管理 ID 集合，
  20 万账号导入十分钟仍未完成。改为批次内共享集合后，20 万账号 73.5s 完成（约 2720 账号/秒）。
+ [修复] 浏览器直接刷新 /accounts、/settings 等前端路由返回 404（history 模式缺少 SPA 回退）；
  现在非 API 路径回退到 index.html，未知 API 路径仍返回 404。
+ [修复] 批量任务一次性提交 N 个 future 造成的队列堵塞、线程堆积与炸机风险。
+ [修复] 批量任务并发被硬编码上限 100 截断，用户设置更大的值也不生效。
+ [修复] 代理节点失效后仍被反复选中、不会切换备用出口：新增失败记录与可用性判定，
  支持熔断、恢复与整组回退直连。
+ [修复] 过载时没有任何自我保护，线程池与并发固定不变导致整机卡死。
+ [修复] 账号列表与导出在大量数据下的全量拷贝与卡顿。
+ [修复] 概览中心缺少小时级精度的统计口径。
+ [修复] 远程更新检测会联网校验版本：本版本彻底移除更新检查、更新任务及其接口、页面与代码。
+ [修复] 顶部「服务」按钮及其弹窗、导航项、外链与文案全部删除，界面不再包含任何原作者入口。
+ [修复] 账号检测与 AT 刷新此前只依赖单次请求内的同步返回，现改为队列化执行并写回同一进度投影，
  坏号不再显示为正常。

### 移除

+ [移除] `/api/system/update-status`、`/api/system/update-task`、`/api/system/update` 及
  `services/update_service.py`、`services/update_status_service.py`、`contracts/updates.py`。
+ [移除] 前端版本更新弹窗、更新检查轮询、版本红点、`api/version.ts`、`lib/release.ts`。
+ [移除] 顶部「服务」按钮、交流与服务弹窗与 `headerServiceItems` 外链配置。
+ [移除] 侧边栏 GitHub 外链、登录页 GitHub 与 Powered by 标识。
+ [移除] `deploy/install.sh`（从原仓库 raw 地址拉取的一键安装脚本）与 `docker-compose.local.yml`、
  `docker-compose.postgres.yml` 两套旧编排；统一为 `docker-compose.yml` 与 `docker-compose.qingmeng.yml`。
+ [移除] 发布流水线中的「在线更新发布包 / checksums」步骤，CI 改为「测试 + 构建 + 多架构推镜像」。

### 文档

+ [新增] `docs/architecture/01-current-architecture.md`：重构前架构剖析与瓶颈归因。
+ [新增] `docs/architecture/02-million-pool-architecture.md`：百万账号分层、选号索引、队列、
+   分页、出口状态机与容量模型。
+ [新增] `docs/architecture/03-adaptive-scheduling.md`：自适应调度器状态机、阈值与可观测字段。
+ [新增] `docs/architecture/04-router-and-contracts.md`：新增/变更/移除的控制面接口对照表。
+ [新增] 重写 `docs/deployment.md`：Docker 部署、DNS 异常处理、离线部署、首次检查清单、
+   压测口径、升级、备份恢复与常见问题。
+ [新增] 重写 `README.md`：问题-方案对照表、能力清单、快速开始、接口表、容量建议与许可说明。


---

## 历史版本（重构前）

以下条目来自重构前的版本线，保留作为行为变更的历史参考。

## 3.2.4 - 2026-10-21

+ [新增] 图片存储支持多个 Cloudflare R2 目标，可在设置中管理目标、测试连接、启用/停用并按图库已管理字节数分配上传；R2 失败时保留本地待同步副本。
+ [优化] 图片资产记录 R2 目标与对象 key，读取、删除、压缩同步和全量同步复用原目标；图片 R2 目标与备份使用的单一 R2 配置保持独立。

## 3.2.3 - 2026-09-09

+ [新增] Python 图片接口和共享模型列表支持 `gpt-image-2.5`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`，通过 ChatGPT Web 的 `auto` 路由兼容，不包含 `exact` 别名。
+ [优化] 默认聊天模型精简为 `gpt-5.6` 和 `auto`，保留现有图片模型；`/v1/models` 与控制台共用模型目录，不再追加上游实时发现的其他模型。
+ [优化] 图片调用日志补齐实际出图分辨率与数量，覆盖普通响应、SSE 和 Studio 异步任务；列表直接展示实际分辨率，请求尺寸、质量和返回格式放在详情中，终端完成日志增加实际宽高，复用已解析的图片信息。
+ [优化] 控制台页面切换增加延迟显示的加载进度与淡入效果，保留侧边栏原有样式。
+ [修复] 手机端侧边栏展开时不再沿用桌面端折叠状态。

## 3.2.2 - 2026-08-11

+ [新增] 设置页新增控制台请求超时、图片首次轮询等待和图片轮询间隔，默认分别为 600 秒、5 秒和 5 秒。
+ [优化] 日志、图片、账号和设置等控制台请求共用可配置的等待上限，登录状态检查保留 8 秒；联网搜索固定使用 10 分钟总时限并额外预留 30 秒返回最终响应，可编辑文件任务固定使用 20 分钟总时限。
+ [优化] 管理员登录或刷新控制台时检查一次新版本；发现更新后 Header 与版本弹窗统一以黄色提示，打开版本弹窗不再重复检查。
+ [优化] 对话画图的参考来源在宽屏中停靠并挤压对话区，窄屏复用统一抽屉；来源列表独立滚动并保留引用高亮。
+ [优化] 概览中心统一使用 12px 卡片圆角，并将模型请求分布的统计口径说明贴近标题展示。
+ [修复] 概览中心调用活跃度在当前统计周期没有请求时展示空态提示。

## 3.2.1 - 2026-08-10

+ [优化] 模型请求分布补充统计口径说明，仅统计成功及部分成功请求。
+ [修复] Dashboard 只在小时聚合表结构不兼容时重建完整投影；仅状态表变化时保留兼容的 30 天历史并重新建立 Call Record 检查点。
+ [修复] 用户删除调用日志后不再触发 Dashboard 全量重建，避免较短的日志保留周期使 7 天和 30 天统计随之收缩。

## 3.2.0 - 2026-08-10

+ [新增] 概览中心新增运行环境面板，展示运行方式、应用版本、实例与系统信息、CPU 与内存占用、数据盘、服务运行时间及网络速率。
+ [新增] 设置页新增概览中心偏好，可设置默认统计周期和 5–300 秒自动刷新间隔；默认使用 24 小时和 10 秒，并保存在当前浏览器。
+ [优化] 概览中心重新组织账号、调用与运行环境信息，保留模型请求分布，并提供调用结果、调用活跃度、成功耗时和模型耗时视图，各图表可独立切换 24 小时、7 天和 30 天。
+ [优化] 概览数据统一由后端生成单次快照；进入页面立即请求一次，页面可见时按设置间隔串行刷新，页面隐藏后停止，后台刷新保留最近一次可用数据。
+ [优化] 统一代码许可证、中英文文档标识和项目元数据为 AGPL-3.0，并保留上游 MIT 版权与许可声明。
+ [修复] 修改概览中心默认周期后，返回已缓存的概览页面会立即应用新设置，不影响页面内各图表的独立切换。
+ [修复] 调用活跃度恢复格子悬浮详情，并在首次进入和切换统计周期时播放进入动画。

## 3.1.1 - 2026-08-08

+ [优化] 概览指标改用增量持久化投影，成功率、成功耗时、模型成功请求分布和当前并发使用统一的数据契约。
+ [修复] GenBox 推送仅接受已登记在 Gallery 索引中的有效图片，推送时携带生成元数据并保留源图片；未登记、外部来源或无效内容不会进入推送队列。
+ [修复] Studio 自动推送在 `CHATGPT2API_BASE_URL` 为空、按当前请求生成图片地址时仍可正常识别来源，同时继续执行来源校验与 Gallery 读取门控。
+ [修复] 可滚动弹层在桌面和窄屏视口保持统一高度，最小化的任务与请求详情面板支持拖拽定位。

## 3.1.0 - 2026-08-07

+ [新增] 管理控制台支持检查 GitHub Release，并在受管 Docker 运行时中执行持久化在线更新；后端统一拥有下载、校验、安装、回滚与重启任务，页面刷新后可继续查看进度。
+ [新增] 手动添加与编辑账号支持填写 AT 和 RT；编辑凭据时使用稳定管理 ID，留空不会覆盖已有凭据。
+ [新增] 账号导入支持选择目标账号组；CPA 与 Sub2API 远程导入全局只运行一个任务，Sub2API 测试连接后直接展示远程分组。
+ [新增] 账号管理支持导入本地 Sub2API JSON 文件，兼容常见导出结构与嵌套凭据字段。
+ [新增] 账号批量处理支持跨分页全选当前筛选结果。
+ [新增] 图片管理支持按配置手动推送到 GenBox，Studio 可在生成成功后通过有界队列自动推送。
+ [优化] 设置页的远程 CPA 与 Sub2API 导入统一使用账号批量操作抽屉展示进度，并支持离开页面后恢复进行中的任务。
+ [优化] 图片与调用日志自动清理统一使用小时保留周期，默认保留 24 小时；日志按精确时间滚动清理。

## 3.0.0 - 2026-08-05

+ [新增] 账号、用户密钥、设置、调用日志和指标统一使用 Application Database，支持 SQLite 与 PostgreSQL 18。
+ [新增] 一键安装支持 SQLite、本地 PostgreSQL 18 容器和已有 PostgreSQL URL，并提供可叠加的 PostgreSQL Compose 配置。
+ [新增] 提供 Chat Completions、Responses、Messages、搜索、图片生成、图片编辑和 PPT / PSD 可编辑文件任务接口。
+ [新增] Vue 3 管理控制台覆盖概览、账号、代理、日志、实时监控、图片管理、对话画图和系统设置。
+ [新增] 账号管理支持 OAuth、Access Token、Session JSON、CPA、远程 CPA 和 Sub2API 导入，以及指定账号的文本与图片测试。
+ [新增] 对话画图支持文本、搜索、文生图、图生图、多图参考、局部编辑、Markdown、任务恢复和本地提示词库。
+ [新增] 图片管理支持本地与 WebDAV 存储、标签、下载、压缩、清理和存储状态查看。
+ [新增] 代理管理支持账号代理、账号组代理、多出口代理组、节点图片并发、默认出口和备用出口。
+ [优化] 账号状态、代理关系、失败分类、日志详情和监控详情统一由后端输出业务投影，前端负责交互与展示。
+ [优化] 批量刷新、同步、导入和账号核验共用账号处理并发设置，批量进度按账号或资源实时展示。
+ [优化] 图片请求统一记录账号、出口、上游错误和各阶段耗时，实时监控与持久化日志复用同一条诊断链路。
+ [优化] 桌面端与移动端统一表格、列表、分页、加载、空状态、菜单、抽屉和滚动规则，列表支持固定视口与随页面展开。
+ [优化] 概览指标采用缓冲写入并保留最近 30 天；调用卡片直接使用汇总与切号统计，调用结果、成功率、成功耗时和切号明细统一按时间展示。
+ [优化] R2 备份覆盖 Application Database，并可按设置包含图片、图片任务与生成文件。
+ [修复] AT 失效时使用有效 RT 刷新；AT 失效且没有可用 RT 的账号不会继续显示为正常或参与调度。
+ [修复] 图片失败统一决定公开错误、重试、账号处置和诊断信息，减少不同接口之间的行为漂移。
+ [修复] Studio 可恢复的图片任务继续轮询原任务，避免重复创建任务或裁剪对话历史。
+ [修复] 文件任务按 API Key 隔离创建、查询和删除，成功产物可通过随机公开链接下载。
+ [修复] 提示词更新请求允许较慢的远端下载，提示词弹窗滚动到底部后可继续加载后续内容。
+ [修复] 一键安装脚本支持通过 `curl | bash` 从标准输入执行，不再因缺少 `BASH_SOURCE[0]` 提前退出。
