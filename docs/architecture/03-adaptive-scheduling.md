# 智能自适应调度器规格

实现：`services/adaptive_governor.py`、`services/hardware_profile.py`
对外接口：`GET /api/system/scheduler`、`POST /api/system/scheduler/mode`、`POST /api/system/scheduler/policy`
界面：设置 → 智能调度与维护

## 1. 目标

1. 过载时自动降配，避免线程堆积、内存打满、整机假死；
2. 恢复后**精准回弹**到用户保存的参数，不做「渐进试探」；
3. 永不修改用户保存的配置（重启后一定从用户预设开始）；
4. 双模式：智能自动 / 手动锁定。

## 2. 输入

### 2.1 硬件画像（启动时一次）

| 字段 | 来源 |
| --- | --- |
| CPU 核心数 | `os.cpu_count()` |
| CPU 型号 | `/proc/cpuinfo` 或 `PROCESSOR_IDENTIFIER` |
| 内存总量 | `/proc/meminfo` 或 `GlobalMemoryStatusEx` |
| 磁盘总量 / 可用 | `shutil.disk_usage` |
| 平台 | `platform` |

分级规则：

| 等级 | 条件 | 推荐账号处理并发 | 推荐检测线程 | 推荐出图线程 | 推荐异步线程池 |
| --- | --- | --- | --- | --- | --- |
| 旗舰 | ≥32 核 且 ≥64GB | 512 | 512 | 64 | 512 |
| 高配 | ≥16 核 且 ≥24GB | 256 | 256 | 32 | 256 |
| 中配 | ≥8 核 且 ≥12GB | 96 | 96 | 16 | 128 |
| 低配 | 其余 | 24 | 24 | 8 | 64 |

> 推荐值只在用户从未保存过对应设置时作为默认值使用；调度器本身不写回任何配置。

### 2.2 实时负载（每次采样）

- CPU：Linux 读 `/proc/stat` 两次求差；Windows 用 `GetSystemTimes` 求差；其他平台退化为 loadavg/核数；
- 内存：`MemTotal - MemAvailable` 或 `ullTotalPhys - ullAvailPhys`；
- 负载均值、磁盘读写速率（Linux）作为参考字段展示；
- 采样不到的维度返回 `None` 并被**忽略**（不会当成 0）。

## 3. 状态机

```
                    overload(cpu>high 或 mem>high)
        normal ───────────────────────────────► warning/critical
           ▲                                        │
           │  healthy(cpu<low 且 mem<low)            │ 每采样一次降一档
           │  连续 hold_seconds(默认300s)            │ 1.0 → 0.75 → 0.5 → 0.35 → 0.2
           └────────────────────────────────────────┘
```

- 降配：每个采样周期最多降一档；`scale` 不低于 `min_scale_percent`（默认 20%）；
- 回弹：健康条件必须**连续**满足 `restore_hold_seconds`；中间任意一次回到阈值带或过载都会重置计时；
- 回弹动作是一次性 `scale = 1.0`（精准回弹用户设置），不是逐档试探；
- 手动模式 / 关闭调度：`scale` 立即固定为 1.0，仅继续采样用于展示。

### 3.1 判定阈值（可网页修改）

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `adaptive.cpu_high` | 85 | CPU 过载阈值 |
| `adaptive.mem_high` | 90 | 内存过载阈值 |
| `adaptive.cpu_low` | 70 | CPU 恢复阈值 |
| `adaptive.mem_low` | 80 | 内存恢复阈值 |
| `adaptive.restore_hold_seconds` | 300 | 恢复需持续的时间 |
| `adaptive.sample_interval_seconds` | 5 | 采样间隔 |
| `adaptive.min_scale_percent` | 20 | 最低降配比例 |
| `adaptive.hardware_override` | auto | 机型覆盖（低/中/高/旗舰） |

约束：`cpu_low` 必须小于 `cpu_high`，`mem_low` 必须小于 `mem_high`；保存时自动修正并做区间校验。

## 4. 生效值

```
effective(key) = max(1, round(user_value * scale))
```

作用于四个可缩放资源：

| key | 用途 |
| --- | --- |
| `account_processing_concurrency` | 账号远程维护并发（预检、检测、刷新、同步） |
| `account_check_workers` | 账号任务队列线程数 |
| `worker_threads` | 异步线程池容量（下限 32，避免事件循环饿死） |
| `image_task_workers` | 出图后台任务线程（展示与建议值） |

用户预设值、当前生效值、比例、最近调节记录都会在 `GET /api/system/scheduler` 中返回，
界面按「用户预设 / 当前生效」两列并排展示，过载时生效列变为琥珀色。

## 5. 监听与副作用

调度器每次状态变化会通知监听者：

- `api/app.py`：调整 anyio 线程池 `total_tokens`，并记录 `runtime_threadpool_adapted` 日志；
- `services/account_processing.py`：每次获取槽位时读取最新生效值（无需监听，天然实时）；
- `services/account_check_queue.py`：管理器每 3 秒按生效值增减工作线程。

## 6. 可观测性

`GET /api/system/scheduler` 返回：

```json
{
  "enabled": true, "mode": "auto", "level": "normal", "scale": 1.0, "scale_percent": 100,
  "watermarks": {...}, "hardware": {...}, "load": {"cpu_percent": 12.4, "memory_percent": 41.2},
  "base": {"account_processing_concurrency": 256, "...": 0},
  "effective": {"account_processing_concurrency": 256, "...": 0},
  "overload_since": null, "healthy_since": null, "restore_in_seconds": null,
  "adjustments": 0, "history": [...], "events": []
}
```

`events` 记录 `downgrade` / `restore` / `pinned` 三类动作及原因，界面展示最近 6 条。

## 7. 测试

`tests/test_adaptive_governor.py` 覆盖：

- 过载单步降配、连续过载到下限、最低比例约束；
- 仅在连续健康满 hold 后回弹；中途波动会重置计时；
- 手动模式与关闭调度固定 1.0；
- 单维度缺失（CPU 或内存）不会误判；
- 生效值下限、监听回调、硬件覆盖投影。

测试通过注入时钟与设置字典，不依赖真实硬件与真实等待。
