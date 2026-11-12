<p align="center">
  <img src="web-vue/public/logo.svg" width="104" alt="倾梦GPT logo" />
</p>

<h1 align="center">倾梦GPT绘画企业级百万号池系统</h1>

<p align="center">
  OpenAI 兼容的文本 / 搜索 / 图像生成接口，底部是一个面向 <strong>100 万上游账号</strong> 设计的高可用号池：
  毫秒级选号、异步检测队列、智能自适应负载调度、代理健康熔断、网页化全量配置与五套独立主题。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v1.9.6-111827" alt="Version v1.9.6" />
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/PostgreSQL-16+-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Docker-qingmeng-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="License AGPL-3.0" />
</p>

<p align="center">
  <a href="./CHANGELOG.md">更新日志</a> ·
  <a href="./docs/architecture/README.md">架构文档</a> ·
  <a href="./docs/deployment.md">部署手册</a>
</p>

> [!IMPORTANT]
> 本项目以 AGPL-3.0-only 分发，并保留上游 MIT 代码的版权与许可声明（见 `LICENSE`、`NOTICE`）。
> 二次开发与私有化部署可以修改品牌、界面与业务逻辑，但不得删除或伪造许可证与版权声明。

## 这套系统解决什么问题

旧架构在 7000+ 账号时就会同时出现下面这些问题，根源是「全表扫描 + 一次性 fan-out + 固定并发」：

| 现象 | 根因 | 本版本做法 |
| --- | --- | --- |
| 选号 2 分钟、偶尔卡死 | 每次请求遍历全表并重建候选列表 | 增量候选索引，按模式/套餐/来源分桶，环形队列出队（p50 ≈ 3µs） |
| 批量检测卡死、线程堆积 | 为每个账号创建一个 future | 有界异步队列，多通道轮转，一次只领一个令牌 |
| 并发上限 100 无法突破 | 硬编码上限 | 上限解除，网页可配，并由调度器动态缩放 |
| 代理失效后一直超时 | 无失败记录与可用性判定 | 健康注册表：熔断 / 冷却恢复 / 整组回退直连 |
| 高并发炸机 | 过载时无自我保护 | 自适应调度：过载降配、健康后精准回弹用户设置 |
| 配置散落在 yml/环境变量 | 没有统一投影 | 全部配置进入网页「设置」，零 yml 操作 |
| 版本更新联网校验 | 远程更新任务 | 已彻底移除，禁止任何联网校验 |

## 核心能力

### 智能自适应百万级调度

- **硬件识别**：启动识别 CPU 核心/型号、内存、磁盘与 IO，自动划分低配 / 中配 / 高配 / 旗舰。
- **动态调节**：CPU > 85% 或内存 > 90% 判定过载，逐级降配防止炸机；CPU < 70% 且内存 < 80%
  连续 300 秒后一次性回弹到用户原始设置。
- **用户预设不可变**：调度只影响运行时生效值，永不回写你保存的并发 / 线程配置。
- **双模式**：智能自动（默认）与手动锁定，一键切换。

### 百万账号号池

- 候选索引 + 管理 ID 反向索引 + 插入序分页窗口；无筛选翻页只读当前页。
- 快照刷新周期随号池规模自适应，避免百万级「整表重载永不停」。
- 检测 / AT 刷新 / 同步统一进异步队列，进度统一投影，可取消、可观测。

### 出口与存储

- 代理组节点并发配额、健康探测、连续失败熔断、冷却后恢复、整组不可用回退直连。
- 图片本地 / WebDAV / R2 多目标存储，R2 备份覆盖 Application Database。
- SQLite 与 PostgreSQL 双支持，内置数据库检查 / 优化 / 重建索引 / 压缩 / 一键修复。

### 控制台

- 概览中心支持 1 小时 / 24 小时 / 7 天 / 30 天四种统计周期。
- 账号分页 20 / 50 / 100 / 500 / 1000 / 5000 条与自定义页大小（上限 10000）。
- 五套完全独立的视觉主题：默认、倾梦·青瓷、星夜·霓虹、樱语·柔光、曜石·极简。
- 全部配置网页可视化，包含调度策略、数据库维护、队列与代理健康。

## 快速开始（Docker，推荐）

```bash
cp .env.qingmeng.example .env      # 设置 QINGMENG_AUTH_KEY
docker compose -f docker-compose.qingmeng.yml up -d --build
docker compose -f docker-compose.qingmeng.yml logs -f app
```

访问 `http://服务器IP:3000`，用 `.env` 中的 `QINGMENG_AUTH_KEY` 登录。

服务器 DNS 异常的机房：构建加 `--network host`，运行期 compose 已内置 `dns:` 与 DNS 自检入口脚本。

镜像名默认 `qingmeng/qingmeng-gpt:1.9.6`（占位 tag，随本版发布构建推送）。完整部署、压测与排障见 [部署手册](./docs/deployment.md)。

## 本地开发

```bash
# 后端（Python 3.13）
uv sync
QINGMENG_AUTH_KEY=dev-key uv run uvicorn main:app --reload

# 前端（另开终端）
cd web-vue && npm install && npm run dev
```

前端构建产物输出到 `web-vue/dist`，容器构建时复制到 `web_dist` 由后端直接托管。

## 测试与基准

```bash
uv run pytest -q                       # 86 项：调度索引 / 队列 / 调度器 / 硬件分级 / 批量导入 / 自动迁移 / 快照刷新 / 更新日志 / 数据库 / API 契约
uv run python scripts/bench_account_selection.py --accounts 1000000 --acquires 200000
```

基准实测（本地开发机）：

```
indexed 1,000,000 accounts in 6.77s (147,740/s)
200,000 acquires in 0.81s (246,863 acquires/s)
selection latency p50 0.0027ms / p95 0.0042ms / p99 0.0057ms
legacy full-pool scan equivalent: 1,000,000 tokens listed in 246.0 ms (per request)
```

## 接口

OpenAI 兼容端点：`/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/v1/images/generations`、
`/v1/images/edits`、`/v1/messages`（Anthropic 兼容）、`/v1/search`。
鉴权：`Authorization: Bearer <用户密钥>`；控制台使用管理员密钥。

控制面新增接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/system/scheduler` | 硬件、负载、生效比例、预设与生效值 |
| POST | `/api/system/scheduler/mode` | 智能自动 / 手动锁定 |
| POST | `/api/system/scheduler/policy` | 阈值、回弹保持、采样间隔、最低比例、机型覆盖 |
| GET/POST | `/api/system/database` `/api/system/database/maintenance` | 数据库状态与一键维护 |
| GET/POST | `/api/system/queue` `/api/system/queue/{id}/cancel` | 账号任务队列观测与取消 |
| GET/POST | `/api/system/proxy-health` `/api/system/proxy-health/probe` | 出口健康与立即探测 |
| POST | `/api/accounts/check` | 批量账号检测（异步队列） |

## 目录结构

```
api/                 路由与契约投影（FastAPI）
contracts/           请求/响应契约与设置规格
services/            业务服务
  account_index.py        选号索引（百万级）
  account_check_queue.py  账号任务异步队列
  adaptive_governor.py    自适应调度器
  hardware_profile.py     硬件识别与负载采样
  proxy_health.py         出口健康与熔断
  database_maintenance.py 数据库维护
  account_service.py      账号池主服务
  protocol/               上游协议实现
web-vue/             Vue 3 控制台
tests/               pytest 测试套件
docs/                架构、地图、手册、ADR
deploy/              Docker 入口与部署脚本
```

## 运维要点

- **容量**：100 万账号建议 32C/128G + PostgreSQL；16C/32G 建议 30 万以内；SQLite 建议 5 万以内。
- **调度**：保持默认「智能自动」，除非你有明确的容量模型需要手动锁定。
- **数据库**：每周执行一次「一键修复」；碎片率超过 20% 会自动压缩。
- **代理**：给代理组配置至少两个出口，健康检查会剔除坏节点并自动恢复。
- **备份**：R2 备份默认包含 Application Database；恢复步骤见部署手册。

## 许可

AGPL-3.0-only。上游 MIT 代码的版权与许可声明保留在 `NOTICE` 与 `LICENSE`。
