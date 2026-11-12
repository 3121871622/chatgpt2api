# 重构前架构剖析（现状与瓶颈归因）

本文记录 v1.0.0 重构前系统的真实结构、数据流与瓶颈点。所有引用均为重构前的模块位置，
用于解释「为什么 7000 账号就会选号 2 分钟」，以及每项修复对应哪一段代码。

## 1. 目录与职责

```
main.py                  uvicorn 入口，create_app()
api/app.py               FastAPI 装配、lifespan 启动线程、
                         CORS、静态资源兜底
api/accounts.py          账号 HTTP 契约（90KB，全部投影函数都在这里）
api/system.py            设置、日志、监控、代理、图片、更新等系统接口
api/ai.py                OpenAI 兼容入口（v1）
services/account_service.py
                         账号池主服务（200KB+）：加载、规范化、选号、
                         检测、AT 刷新、导入导出、进度、存储读写
services/openai_backend_api.py
                         上游 HTTP 会话与图片/文本调用
services/protocol/*      协议适配（chat / responses / anthropic / search）
services/proxy_service.py出口解析、代理组配额、Cloudflare 清关
services/storage/*       账号/密钥仓储（默认 SQLAlchemy + SQLite/PG）
web-vue/                 Vue 3 控制台（127 个 ts + 102 个 vue）
```

数据形态：`accounts` 表只有 `id / access_token / data(JSON)` 三列，**账号的所有业务字段都在 JSON 里**。
进程启动时全表读取、逐条规范化为 `dict`，常驻内存 `self._accounts: dict[token, dict]`。

## 2. 关键路径

### 2.1 选号（图片）

重构前的 `AccountService.get_available_access_token` → `_acquire_next_candidate_token`：

```text
每次请求：
  1. _refresh_accounts_snapshot_if_stale()      # TTL 5s，可能全表重载
  2. _list_ready_candidate_tokens()             # 遍历全池 + 逐条鉴权可用性判断
  3. _list_available_candidate_tokens()         # 再遍历一次，过滤在途并发
  4. tokens[self._index % len(tokens)]          # 轮询取一个
  5. 在途已满 -> wait(1s) -> 回到 1
```

复杂度是 **O(n) 每请求**，而且在外层还有最多 20 次重试、每次重试都再扫两遍。
7000 账号时一次选号约 14k 次字典取值 + 2 次列表构建；100 万账号时是 200 万次/请求。

更致命的是 `wait(1s)` 之后会调用 `_refresh_accounts_snapshot_if_stale(wait_for_refresh=True)`，
高并发下所有等待线程会周期性触发全表重载，形成「刷新风暴」。

**归因：热路径上做了全量计算。**

### 2.2 批量检测 / AT 刷新

`sync_accounts_and_quota(tokens)`：

```text
executor = ThreadPoolExecutor(max_workers=account_processing_worker_count(len(tokens)))
for token in tokens:                      # 一次性提交全部
    futures[executor.submit(fetch_remote_info, token)] = ...
for future in as_completed(futures): ...
```

问题：

1. 选择 5 万账号 → 5 万个 future + 5 万个 limiter 等待者，内存与线程调度压力巨大；
2. 该调用本身占住一个 `run_in_threadpool` 线程直到全部完成（最长数小时）；
3. 失败重试、取消、进度都只能靠外层轮询；
4. 检测与刷新共用同一个入口，AT 刷新会排在长检测队列之后。

**归因：用「一次性 fan-out」实现批量任务。**

### 2.3 并发上限

```python
_MAX_CONCURRENCY = 100
return max(1, min(_MAX_CONCURRENCY, value))
```

用户把并发设成 500 也只生效 100；而且值固定，机器过载时不会下降。

**归因：把「安全上限」写成了常量，而不是可观测的运行时策略。**

### 2.4 代理

`ProxySettingsStore._resolve_proxy_group` 只在组内按容量随机选节点，**没有任何失败记录**：

- 节点连不上 → 请求超时 → 下一个请求仍然随机选中它；
- 整组不可用时抛 `ProxyReferenceUnavailableError`，不会尝试直连；
- 备用出口只在「早期连接失败」时触发一次，且要求显式配置 `fallback_proxy`。

**归因：缺少出口状态机。**

### 2.5 数据库

- 每行一个 JSON blob，任何按字段的筛选都只能在内存做；
- SQLite 单写者 + WAL 长期运行会累积空闲页，没有维护入口；
- 快照 TTL 固定 5s，与池大小无关。

**归因：存储层没有任何「规模」概念。**

### 2.6 控制台

- 顶部「服务」按钮、版本更新弹窗、导航项里包含原作者外链与在线更新；
- 配置分散在环境变量、`config.json`、数据库三处；
- 主题只有亮/暗两种；
- 概览只有 24h / 7d / 30d。

**归因：面向个人部署的控制台，没有企业运维视角。**

## 3. 瓶颈排序（重构前）

| 排名 | 瓶颈 | 触发条件 | 表现 |
| --- | --- | --- | --- |
| 1 | 选号全表扫描 | 每次图片请求 | 选号数秒到分钟级 |
| 2 | 批量 fan-out | 批量检测/刷新 | 线程堆积、内存上涨、接口假死 |
| 3 | 固定并发 100 | 高并发 | 吞吐上不去 |
| 4 | 快照 5s 全量重载 | 大池 | 周期性卡顿 |
| 5 | 代理无状态 | 节点抖动 | 持续超时、不切换 |
| 6 | 列表全量拷贝 | 打开账号页 | 页面卡顿、内存峰值 |
| 7 | 无自我保护 | 高并发/大盘同步 | CPU 打满、整机假死 |

## 4. 重构原则

1. **热路径只做 O(1)**：选号、取号、分页窗口、ID 查账号。
2. **长尾任务进队列**：批量操作只入队，不占请求线程。
3. **状态外置**：出口健康、任务进度、调度比例都是显式状态，可观测、可恢复。
4. **配置单源**：网页设置为唯一可写源，环境变量只保留引导项。
5. **降级可预期**：过载降配、代理熔断、整组回退直连，都是设计好的路径而非异常。
