# 控制面契约与前端对应关系

本文件只列 v1.0.0 新增或变更的控制面接口，以及前端调用位置。完整接口见 `api/`。

## 1. 调度

| 接口 | 请求 | 响应 | 前端 |
| --- | --- | --- | --- |
| `GET /api/system/scheduler` | — | `SchedulerSnapshot` | `SettingsOpsPanel.vue`（5s 轮询） |
| `POST /api/system/scheduler/mode` | `{mode: auto\|manual, enabled?: bool}` | `SchedulerSnapshot` | 同页面模式按钮 |
| `POST /api/system/scheduler/policy` | `AdaptiveSchedulerPatch` 全字段 | `SchedulerSnapshot` | 同页面「保存策略」 |

`SchedulerSnapshot` 关键字段：`scale` / `scale_percent` / `level` / `watermarks` / `hardware` /
`load` / `base` / `effective` / `events` / `restore_in_seconds`。

单位约定：百分比为 0-100 的数字；`restore_hold_seconds`、`sample_interval_seconds` 为秒。

## 2. 数据库维护

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/system/database` | — | `DatabaseMaintenanceStatus` |
| `POST /api/system/database/maintenance` | `{operations: ["check"\|"optimize"\|"reindex"\|"compact"\|"repair"]}` | `DatabaseMaintenanceResult` |

- 未知操作返回 409；
- 已有维护在跑时返回 409（`database maintenance is already running`）；
- `steps[]` 每条包含 `statement / ok / duration_ms / detail`，界面直接展示。

## 3. 任务队列

| 接口 | 说明 |
| --- | --- |
| `GET /api/system/queue` | `{stats, jobs[]}`，`stats` 含 workers / worker_target / queued_tokens / processed_total |
| `POST /api/system/queue/{job_id}/cancel` | 取消排队中或运行中的任务；不存在或已结束返回 404 |
| `POST /api/accounts/check` | 提交批量检测任务，返回 `progress_id` + `job_id`，进度沿用既有 `refresh_progress` 投影 |

前端：`SettingsOpsPanel.vue` 展示最近 6 个任务与取消按钮；账号页批量操作走
`accountsApi.checkAccounts(target, onProgress)`。

## 4. 代理健康

| 接口 | 说明 |
| --- | --- |
| `GET /api/system/proxy-health` | 节点状态（healthy / degraded / down）、冷却剩余、回退引用 |
| `POST /api/system/proxy-health/probe` | 立即探测所有代理组节点 |

## 5. 设置契约

`SettingsPatch` 新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_processing_concurrency` | int ≥1（无上限） | 账号处理并发 |
| `worker_threads` | int 8-8192 | 异步线程池 |
| `account_check_workers` | int 1-8192 | 检测队列线程 |
| `image_task_workers` | int 1-1024 | 出图任务线程 |
| `adaptive.*` | 见调度规格 | 自适应策略 |

`SettingsView.fields` 同步新增元数据（默认值 / 区间 / 单位），前端据此渲染输入框。

## 6. 概览

`GET /api/dashboard`：

- `meta.available_ranges` 现在为 `["1h","24h","7d","30d"]`；
- `ranges["1h"].window.bucket_count = 1`、`bucket_unit = "hour"`；
- 前端 `api/stats.ts` 的契约校验同步放宽，`lib/timeRanges.ts` 增加「1 小时」选项。

## 7. 已移除

| 接口 | 状态 |
| --- | --- |
| `GET /api/system/update-status` | 移除（404） |
| `GET /api/system/update-task` | 移除（404） |
| `POST /api/system/update` | 移除（不再注册路由） |

对应前端 `api/version.ts`、`lib/release.ts`、版本弹窗全部删除。
