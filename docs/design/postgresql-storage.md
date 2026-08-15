# PostgreSQL 存储决策

> 状态：已接受
>
> 决策日期：2026-08-16
>
> 适用版本：`v0.1-alpha` 起

## 1. 决策

NEKO-ONE 第一版使用 PostgreSQL 作为公共房间业务数据的唯一事实源，不再把
SQLite 作为生产运行后端。第一版仍保持单应用实例、单房间主写入者，不引入
Redis。

PostgreSQL 负责持久化：

- 游客身份、状态和最后活动时间；
- 房间状态与严格递增的 `room_seq`；
- 公共消息、回放事件和客户端幂等请求；
- generation/turn 状态与故障结果；
- 审核日志、房间控制、限额和保留策略设置。

Live2D 文件、会话签名密钥和短期共享 TTS 音频仍保存在受权限保护的本地数据
目录。它们不是关系型业务数据，也不得进入公开 Web 根目录。

## 2. Memory Service 边界

公共房间只通过 `MemoryFacade` 调用 Memory Service 的受限内部 API。Recent、
Facts、Reflections 与 Persona 的内部布局仍由 Memory Service 自己负责；
`neko-api` 不读取其原始 SQLite、JSON 或 Persona 文件。

本决策中的“采用 PostgreSQL”首先替换公共房间原有的 `RoomStore` SQLite。
Memory Service 的内部存储如果以后迁移，必须另做兼容、语义校验和回滚决策，
不能用复制表文件或替换连接字符串冒充迁移完成。

## 3. 为什么第一版不需要 Redis

单实例内的 `RoomConnectionHub`、`RoomDirector`、发言队列、在线状态和限流均由
同一进程掌握。引入 Redis 不会增加这套拓扑的正确性，反而会增加故障域和恢复
步骤。

只有出现多实例、滚动发布或故障转移需求时，才引入 Redis，并且仅用于：

- 跨实例发布与短期在线状态；
- 限流计数和短期队列协调；
- 带 TTL 的房间 owner lease。

多实例写入必须同时使用单调 fencing token。只有 Redis 锁而没有数据库端
fencing 校验，不能满足“同一时刻只有一个房间主写入者”的约束。消息、记忆、
审核和正式配置永远不能只保存在 Redis。

## 4. 数据库约束

- 生产启动必须显式提供 `NEKO_PUBLIC_DATABASE_URL`；不允许静默回退到 SQLite。
- 数据库用户只拥有 NEKO 专用数据库/schema 的连接和读写权限，不得是
  PostgreSQL 超级用户。
- `room_seq` 的读取、递增、消息/事件写入必须处于同一事务，并锁定对应房间行。
- `(room_id, room_seq)` 与 `(visitor_id, request_id)` 必须保持唯一。
- schema 通过版本化、只向前的迁移管理；应用启动不执行破坏性降级。
- 就绪探针必须验证连接、schema 版本和可回滚写入，不返回 DSN、主机名或凭据。

## 5. SQLite 数据迁移

已有 SQLite 数据只能通过一次性校验式迁移工具导入 PostgreSQL：

1. 停止旧公共房间写入并创建一致 SQLite 快照；
2. 在 PostgreSQL 空 schema 中执行版本迁移；
3. 按外键依赖顺序导入全部业务表；
4. 比对逐表数量、房间 `last_seq`、关键唯一键和规范化内容摘要；
5. 抽样检查消息、事件、审核和设置语义；
6. 通过后切换应用 DSN，SQLite 快照只读封存一个回滚窗口。

任一数量、摘要、序号或引用校验失败都必须中止切换。回滚方式是停止新应用、
保留 PostgreSQL 失败现场并重新启用只读封存的旧版本；禁止新旧数据库双写。

## 6. Debian 部署与恢复

目标 Debian 主机安装 PostgreSQL 服务端与客户端工具。PostgreSQL 只监听本机
或私有网络；公网仍只开放 TCP 443。数据库备份使用 `pg_dump` 自定义格式，恢复
使用 `pg_restore` 写入新的空数据库，并在切换前运行 schema、数量和业务不变量
校验。

Memory Service 数据、本地密钥、Live2D 和 TTS 文件继续进入独立加密文件备份。
PostgreSQL dump 与文件备份必须共享同一演练编号和时间窗口，恢复记录中分别
保存校验结果。

## 7. 验收条件

- Debian CI 在真实 PostgreSQL 实例上通过全部公共房间行为与容量基线；
- 服务缺少或无法连接 PostgreSQL 时拒绝就绪，不产生 SQLite 数据库；
- 并发提交下 `room_seq` 无重复、无缺口且始终只有一个 generation；
- SQLite 导入工具能成功迁移基准数据，并拒绝损坏、非空目标或校验不一致；
- `pg_dump`/`pg_restore` 隔离恢复后，消息、事件、身份、审核和设置均一致；
- 部署文档明确说明 PostgreSQL 不对公网开放，且不依赖 Redis。
