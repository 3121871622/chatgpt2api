# 架构文档

本目录记录倾梦GPT百万号池系统的架构事实：现状分析、目标设计、关键机制与验收口径。
所有结论都对应到具体模块，便于后续维护者按图索骥。

| 文档 | 内容 |
| --- | --- |
| [01-current-architecture.md](./01-current-architecture.md) | 重构前架构的完整剖析与瓶颈归因 |
| [02-million-pool-architecture.md](./02-million-pool-architecture.md) | 百万账号目标架构、数据流、容量模型 |
| [03-adaptive-scheduling.md](./03-adaptive-scheduling.md) | 智能自适应调度器规格与状态机 |
| [04-router-and-contracts.md](./04-router-and-contracts.md) | 控制面 API 契约与前端对应关系 |

相关文档：

- [部署手册](../deployment.md)
- [系统地图](../maps/system-map.md) / [后端地图](../maps/backend-map.md) / [前端地图](../maps/frontend-map.md)
- [控制台架构](../control-panel-architecture.md) / [数据契约](../control-panel-data-contract.md) / [性能](../control-panel-performance.md)

## 一页纸结论

1. 选号是热路径，必须 O(1)。任何「每次请求遍历全池」的实现都会在万级账号时崩溃。
2. 批量任务是长尾任务，必须队列化并限流。一次性 fan-out N 个 future 等于把 N 倍内存和线程压力同时压进进程。
3. 并发上限必须由「用户预设 × 运行时空闲度」决定，而不是写死常量。
4. 出口需要状态机（健康 / 观察 / 熔断 / 冷却），否则坏节点会被无限重试。
5. 配置必须只有一个来源（网页设置），环境变量只保留引导必需项（密钥、数据库地址、数据目录）。
6. 百万级不是「把参数调大」，而是把每次操作的复杂度从 O(n) 降到 O(1) 或 O(page)。
