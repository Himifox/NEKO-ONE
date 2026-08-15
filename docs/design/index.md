# NEKO ONE 设计文档

当前仓库只维护公共房间 Web 第一版。历史桌面端、插件、游戏、替代渲染器和通用 Agent 文档已经移除。

## 当前设计

- [分阶段实施计划](./implementation-plan.md)：第一阶段至 1.0 的实施范围、交付物、退出条件和当前执行顺序。
- [产品路线图](../roadmap.md)：从单房间 Alpha、公网 Beta 到身份、多房间和 1.0 的阶段目标与验收门槛。
- [公共房间 Web 架构](./public-room-web-architecture.md)：产品边界、组件职责、消息协议、身份、安全、部署与迁移计划。
- [数据保留 ADR](./retention-policy.md)：消息、访客记忆、审计、共享语音的期限、清理顺序和失败重试。
- [公网边界与依赖降级 ADR](./public-edge-security.md)：Nginx、浏览器安全策略、请求限制和 LLM/Memory/TTS 故障语义。
- [长期记忆架构](../architecture/memory-system.md)：Recent → Facts → Reflections → Persona 的证据链与召回流程。
- [容量与稳定性验证](../operations/capacity-and-soak.md)：10/25/50 人负载、慢连接隔离和 24 小时 soak 的执行门槛。
- [备份与恢复](../operations/backup-and-restore.md)：一致性快照、清单、隔离恢复和异机演练的操作约束。
- [公网资产授权](../operations/public-assets.md)：模型、声音、字体和浏览器运行库的分发边界。
- [真实供应商验收](../operations/provider-acceptance.md)：LLM、Memory、TTS 的费用闸门、隔离数据与证据格式。

代码、部署配置和验证脚本是最终实现依据。架构发生变化时，应同步更新上述文档。
