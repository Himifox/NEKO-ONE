# NEKO ONE 设计文档

当前仓库只维护公共房间 Web 第一版。历史桌面端、插件、游戏、替代渲染器和通用 Agent 文档已经移除。

## 当前设计

- [产品路线图](../roadmap.md)：从单房间 Alpha、公网 Beta 到身份、多房间和 1.0 的阶段目标与验收门槛。
- [公共房间 Web 架构](./public-room-web-architecture.md)：产品边界、组件职责、消息协议、身份、安全、部署与迁移计划。
- [数据保留 ADR](./retention-policy.md)：消息、访客记忆、审计、共享语音的期限、清理顺序和失败重试。
- [长期记忆架构](../architecture/memory-system.md)：Recent → Facts → Reflections → Persona 的证据链与召回流程。

代码、部署配置和验证脚本是最终实现依据。架构发生变化时，应同步更新上述文档。
