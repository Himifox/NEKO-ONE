# NEKO 公共房间 Web 版架构设计

> 状态：已接受；v0.1 代码基线已实现，公网验收尚未完成
> 日期：2026-08-15  
> 适用范围：面向公网、单个 NEKO 角色、10–50 人公共房间  
> 非当前桌面版运行时契约；实现完成并通过验收前，不应将本文描述为已上线能力。

## 1. 决策摘要

第一版直接以当前可运行的 N.E.K.O 代码为底座做增、删、改、查，不从空仓库重写。最终代码树只保留形成 NEKO 人格连续性和公共房间体验所需的能力：文本对话、Persona、长期记忆、Live2D、情绪、流式输出、TTS、多人连接和私有管理。

如果产品需要使用新的仓库名，应从现有仓库分支或保留历史迁出，而不是建立没有历史的空仓库。第一版的实施对象始终是现有代码：复用已经工作的 provider、Memory、Live2D 和 TTS 路径，在原位置解除桌面/单用户耦合，确认引用清零后删除其余模块与资产。

第一版采用**模块化单体 + 内部记忆服务**：

- `neko-api` 是房间状态和消息顺序的唯一权威写者；
- RoomConnectionHub、RoomTurnQueue、ConversationEngine、RoomDirector、AvatarState 先作为 `neko-api` 内部模块运行；
- `memory-service` 保持独立进程，只允许内网访问；
- `tts-worker` 可按资源情况独立，也可先以内嵌适配器运行；
- Caddy/Nginx 是唯一公网入口，公网只开放 443；
- 单实例使用 SQLite/文件存储，出现多实例需求后再引入 PostgreSQL、Redis 和对象存储。

模块边界从第一天写清楚，但第一版不为“看起来像微服务”支付分布式事务和队列运维成本。

### 1.1 改造原则

1. **先收窄入口，再删除实现**：先把 Main Server 改成公共产品路由白名单，保证无关能力不可达；随后按依赖顺序删除路由、业务模块、前端入口、资产和依赖声明。
2. **复用能力，不保留生态**：复用 LLM、Memory、Live2D、TTS 的核心实现，不为插件、桌面启动器、其他渲染器或游戏保留兼容壳。
3. **单链路替换**：新增房间链路接管正式入口后删除旧 WebSocket 单连接链路，不长期维护 public/legacy 两套协议。
4. **删除必须可证明**：每批删除前检查 import、路由、配置、文档、构建脚本和静态引用；删除后不能残留静默失效的按钮、配置项或环境变量。
5. **管理能力内收**：原本面向本地用户的配置接口改成私有后台 CRUD，不直接把现有 `config_router`、`memory_router` 整组暴露公网。
6. **每步保持可启动**：增删改按垂直链路推进，每完成一批都应能启动公共页面并完成最小文字对话。

### 1.2 产品定位

> 一个拥有身体、连续记忆，能辨认不同访客并主持公共房间的长期在线实体。

### 1.3 第一版硬约束

1. 同一房间同一时刻最多有一个 NEKO 回复在生成。
2. 同一房间只有一个权威写者和一个 RoomDirector 定时器。
3. 所有可恢复的房间事件使用单调递增的 `room_seq`。
4. 浏览器提交必须携带幂等键，重试不得产生重复消息。
5. Persona 只能由管理员修改，任何对话内容都不能自动写入 Persona。
6. 访客独立记忆默认不向其他访客披露。
7. LLM、TTS、Memory 和管理凭据永不下发浏览器。
8. 模型、TTS 或记忆服务故障不能破坏已经提交的消息顺序。

## 2. 目标和非目标

### 2.1 第一版目标

- 10–50 人同时在线的小型公共房间；
- 访客身份稳定、可封禁、可限流、可选择登录；
- 公共时间线、流式回复、回复对象和排队状态；
- 断线重连后按序补发已提交消息；
- 服务端统一调度 NEKO 的发言，避免刷屏、抢话和重复；
- 一次生成 Live2D 情绪状态与 TTS，广播给全部客户端；
- 房间共享记忆、访客独立记忆和受保护 Persona；
- 私有后台完成配置、审核、删除、封禁、配额和日志查询；
- 单台 VPS 可部署、备份和恢复。

### 2.2 第一版明确不做

- VRM、MMD、PNGTuber、VMC；
- 麦克风、ASR、实时语音输入和语音克隆；
- Agent Server、浏览器/电脑控制、OpenFang 和插件市场；
- 游戏、Galgame、卡片、OCR、桌面小组件、教程和成就；
- Bilibili、Twitch、抖音、礼物、SC、舰长等直播平台事件；
- 多角色、多房间跨区调度和水平扩容；
- 端到端私聊。公共房间里的 NEKO 输出默认对全房间可见。

“不做”不是仅在 UI 隐藏。第一版交付代码中，与上述能力专用的路由、模块、依赖、模板、静态资产、配置字段和启动项都应删除；只有被保留能力仍然依赖的通用工具可以留下，并需要在代码归属上改为中性名称。

### 2.3 对现有工程的操作定义

- **增**：多人身份、RoomConnectionHub、RoomTurnQueue、RoomDirector、房间事件存储、重连续传、管理权限与审计。
- **删**：Agent、插件、桌面、游戏、直播平台、替代渲染器、ASR/麦克风及其专用代码和资产。
- **改**：Main Server 路由白名单、单连接 LLMSessionManager、WebSocket 协议、`master_name` 提示词、记忆作用域、TTS 广播和现有首页。
- **查/管**：将 Persona、记忆、模型、Live2D、封禁、额度和日志做成有权限与审计的后台查询及 CRUD。

实施时不新增第二套同功能 provider、第二套 Memory 存储或第二套 Live2D 加载器。已有实现不能直接复用时，先把其中可复用部分原地解耦，再删除旧调用面。

## 3. 系统上下文与信任边界

```text
Browser
  | HTTPS / WSS
  v
Caddy or Nginx  [唯一公网入口]
  |-- 静态网页 / Live2D 资源
  `-- /api/*, /ws
          |
          v
      neko-api  [房间权威写者]
       |   |  \
       |   |   `-- LLM provider APIs
       |   `------ tts-worker / TTS provider
       `---------- memory-service
                       |
                       `-- 私有持久化卷

Admin Browser
  | HTTPS + 强认证（可再叠加 VPN/IP allowlist）
  `-- /admin/* -> neko-api -> internal services
```

信任边界：

- 浏览器、访客输入和 WebSocket 元数据均不可信；
- 反向代理到 `neko-api` 的身份头只在可信代理网络内接受；
- LLM 输出仍是不可信文本，渲染前必须净化；
- Memory 与 TTS 是内部服务，不因“内网”而跳过鉴权；
- 管理后台和公共 API 使用不同路由、权限与审计策略。

## 4. 逻辑组件

### 4.1 RoomConnectionHub

职责：

- 维护 `connection_id -> visitor_id` 的在线连接；
- 处理连接鉴权、心跳、背压和断线清理；
- 广播房间事件，向单个连接发送私有确认；
- 根据客户端的 `after_seq` 补发持久事件；
- 在 `session.ready`、事件回放和活动流快照入队前保持连接 inactive，禁止实时广播越过启动屏障；
- 对慢客户端设置有界发送队列，超限后主动断开并要求重连，而不是拖慢全房间。

Hub 不决定回复谁，不调用模型，也不直接修改长期记忆。

### 4.2 RoomTurnQueue

职责：

- 接收已持久化的访客消息作为候选 turn；
- 保证任一时刻最多一个活动 turn；
- 合并短时间内同一访客的连续补充；
- 记录排队、选中、取消、超时和完成状态；
- 为 RoomDirector 提供候选集合，不把简单 FIFO 当作最终发言策略。

队列状态以服务端为准。客户端显示的位置仅是提示，不承诺严格的先到先得。

### 4.3 ConversationEngine

职责：

- 构造带明确边界和预算的模型上下文；
- 调用多供应商 LLM 适配器；
- 解析流式文本、情绪标签和结构化结束信息；
- 处理超时、取消、供应商限流和降级；
- 只返回生成结果，不自行广播、不直接提交房间消息。

第一版禁止通用工具调用。显式记忆召回由受控的 `MemoryFacade` 完成，不把任意 URL、文件或系统工具暴露给模型。

### 4.4 MemoryFacade

职责：

- 将公共房间的数据模型映射到现有 Recent → Facts → Reflections → Persona 能力；
- 强制房间、访客和 Persona 三层作用域；
- 进行写入策略、检索预算、可见性和敏感信息过滤；
- 为 Memory 服务失败提供超时、熔断和空结果降级；
- 给所有写入附加来源消息、主体、置信度和审核状态。

所有调用必须使用稳定内部 API；`neko-api` 不读取或修改 Memory 服务的原始 JSON/SQLite 文件。

### 4.5 RoomDirector

RoomDirector 是公共房间唯一的“什么时候说、回复谁、是否保持沉默”的决策者。它从 NEKO Live 提取策略，但不保留直播平台耦合。

输入：

- 候选消息、提及关系、等待时间；
- 最近发言人和近期被回复次数；
- 消息相似度、主题、风险和语言；
- 当前生成状态、冷却时间、房间活跃度；
- 管理员设置的主持模式与频率上限。

输出：

- `reply(message_ids, target_visitor_id)`；
- `defer(until, reason)`；
- `drop(message_ids, reason)`；
- `proactive(topic_seed, reason)`；
- `stay_silent(reason)`。

第一版使用可解释的规则评分，不额外调用一个 LLM 决定“回复谁”：

1. 先剔除已消费、已撤回、被审核拦截、超时或高度重复的候选；
2. 明确提及 NEKO、管理员点选和对 NEKO 的直接追问获得硬优先级；
3. 普通候选按等待时间、话题相关性和当前互动价值加分；
4. 同一访客连续被回复、近期相似话题和过长文本扣分；
5. 等待时间必须提供逐步上升的保底分，避免活跃访客永久淹没安静访客；
6. 同分时使用最早服务端接收序号，保证结果可复现。

每次决定保存特征快照、最终分数和 `reason_code`，但不保存模型推测的敏感用户画像。阈值属于可版本化配置，配置变化只影响之后的 turn。

浏览器端不得运行主动搭话计时器。主动主持由服务器唯一 Director 产生，并与用户回复共用同一队列和冷却约束。

### 4.6 AvatarState

职责：

- 把模型情绪映射为白名单内的 Live2D 表情/动作；
- 保持房间级当前状态及过期时间；
- 广播 `avatar.state`，让新连接可获取快照；
- 对未知、冲突或超长动作回落到中性状态。

客户端只执行服务端批准的动作标识，不执行模型生成的脚本、路径或任意参数。

### 4.7 SpeechService

职责：

- 基于最终或稳定句段生成一次音频；
- 用 `assistant_message_id + voice_config_version + text_hash` 去重；
- 保存音频元数据，向全部客户端广播同一资源；
- 失败时仅发送 `speech.failed`，不撤回文字回复；
- 将声音选择和供应商凭据限定在管理端。

第一版优先“文字先完成，TTS 随后可播放”，避免音频生成阻塞房间 turn 的释放。

## 5. 房间状态机与并发模型

### 5.1 房间状态

```text
IDLE
  | Director 选中候选
  v
PREPARING  --记忆/模型不可用--> RECOVERING --> IDLE
  |
  v
GENERATING --取消/超时--------> FINALIZING
  |                              |
  `------------------------------'
                 |
                 v
              COOLDOWN
                 |
                 v
                IDLE
```

状态转换只能由房间 actor/event-loop 串行执行。HTTP 管理操作、WebSocket 输入和后台定时器均投递命令，不直接改房间内存状态。

### 5.2 单写者实现

第一版每个房间由一个进程内 actor 持有：

- 一个有界命令队列；
- 一个活动 generation；
- 一个 RoomDirector 定时器；
- 一个 SQLite 写事务入口；
- 一个递增 `room_seq` 分配器。

即使第一版只有一个房间，也保留 `room_id` 维度，避免数据表和协议日后破坏性迁移。

实现使用每房间 event lock 把“分配 `room_seq`、事务提交、实时广播”串成一个临界区，保证不同访客并发提交时客户端仍按提交序收到持久事件。连接回放按 `generation lock → event lock` 的固定顺序取得一致切面；流式 delta、活动快照和终止事件共用 generation lock，避免快照已包含某段文字后又收到重复 delta。

进程重启后：

1. 从数据库恢复最后 `room_seq`、最近消息与未完成 turn；
2. 未完成 generation 标记为 `interrupted`，不尝试拼接旧供应商流；
3. 广播一条持久化的恢复事件；
4. Director 重新评估尚未消费的访客消息。

### 5.3 多实例演进

只有在确需水平扩容时才引入：

- PostgreSQL：持久消息、事件、身份和审核状态；
- Redis：房间租约、短期在线状态、跨实例发布；
- 对象存储：TTS 和静态资产；
- `room_id -> owner_instance` 租约与 fencing token。

数据库写入必须校验 fencing token，防止租约切换后旧实例继续写入。仅增加 Redis 分布式锁但不加 fencing token，不能满足单写者约束。

## 6. 核心消息流程

### 6.1 访客发言

1. 客户端发送 `chat.send`，携带 `request_id` 和文本。
2. 服务端从认证上下文取得 `visitor_id`，忽略客户端伪造的身份字段。
3. 校验 Origin、房间权限、封禁、频率、长度和幂等键。
4. 在一个事务中写入消息、分配 `room_seq`、记录幂等结果。
5. 广播持久事件 `message.created`。
6. 将消息投递给 RoomTurnQueue。
7. 私下向发送者确认 `chat.accepted`，包含 `message_id` 和队列状态。

重复 `request_id` 返回首次结果，不重复广播、不重复入队。

### 6.2 NEKO 回复

1. Director 从候选中选择目标和证据消息集合。
2. 写入 `turn.started`，冻结本轮输入消息 ID；后到消息进入下一轮候选。
3. MemoryFacade 按预算检索三层上下文。
4. ConversationEngine 生成，Hub 广播临时 `stream.started/delta`。
5. 流式内容仅用于即时显示，不逐 token 写数据库。
6. 生成结束后净化文本，在一个事务中写入 NEKO 消息和 `message.created`。
7. 广播 `stream.completed`，其 `message_id` 指向持久化结果。
8. 释放 turn，进入冷却；记忆写入和 TTS 进入可重试后台任务。

如果流在最终提交前失败，发送 `stream.failed`；已显示的临时文本从 UI 的正式时间线移除或标为未完成，不伪装成已提交消息。

### 6.3 主动主持

Director 只有同时满足以下条件才可创建主动 turn：

- 房间存在在线访客；
- 没有生成和高优先级排队消息；
- 已超过最小沉默时间；
- 未超过小时/分钟频率预算；
- 主题与近期 NEKO 发言不重复；
- 最近一次主动发言获得互动，或仍在允许的有限试探次数内；
- 管理员未暂停主持。

主动 turn 必须记录 `reason_code`，例如 `dead_air`, `topic_followup`, `welcome_cluster`。不得使用单纯随机定时器掩盖策略缺失。

## 7. WebSocket 协议

### 7.1 连接

建议端点：`GET /ws/rooms/{room_id}?after_seq=<n>`。

认证优先使用安全 Cookie；若使用短期 WebSocket ticket，应先通过 HTTPS 获取并设置极短有效期，避免长期令牌出现在 URL、代理日志或浏览器历史中。

连接成功后服务端发送：

```json
{
  "type": "session.ready",
  "protocol_version": 1,
  "connection_id": "conn_...",
  "room_id": "main",
  "visitor": {"id": "vis_...", "display_name": "Guest 42"},
  "last_room_seq": 1842,
  "heartbeat_interval_ms": 25000,
  "server_time": "2026-08-15T10:00:00Z"
}
```

随后按需要发送 `room.snapshot`、缺失的持久事件以及活动 generation 的 `stream.snapshot`。

### 7.2 信封

客户端命令：

```json
{
  "type": "chat.send",
  "request_id": "01J...",
  "client_time": "2026-08-15T10:00:03Z",
  "payload": {"text": "晚上好"}
}
```

服务端事件：

```json
{
  "type": "message.created",
  "event_id": "evt_...",
  "room_seq": 1843,
  "server_time": "2026-08-15T10:00:04Z",
  "payload": {}
}
```

规则：

- `protocol_version` 在连接阶段协商；不兼容版本明确拒绝；
- 持久事件带 `room_seq`，临时流事件不占用 `room_seq`；
- 临时流事件带 `generation_id` 与递增 `chunk_index`；
- 未知事件类型可忽略，未知必填字段或超出大小限制则拒绝；
- 所有时间使用 UTC RFC 3339，排序以 `room_seq` 而非客户端时间为准；
- 单条文本、单帧和单连接速率均有上限。

### 7.3 第一版事件清单

持久广播事件：

- `message.created`
- `message.moderated`
- `turn.started`
- `turn.interrupted`
- `room.control.updated`
- `room.notice`

临时广播事件：

- `presence.updated`
- `queue.updated`
- `stream.started`
- `stream.delta`
- `stream.snapshot`
- `stream.completed`
- `stream.failed`
- `avatar.state`
- `speech.ready`
- `speech.failed`

单连接事件：

- `replay.reset`
- `chat.accepted`
- `command.rejected`
- `rate_limit.changed`
- `session.revoked`

### 7.4 重连与续传

- 客户端持久保存最后完整处理的 `room_seq`；
- 重连请求携带 `after_seq`；
- 服务端从事件保留窗口补发；
- `session.ready` 返回 `oldest_available_seq`；客户端游标超出保留窗口或领先于服务器时，服务端发送 `replay.reset`，客户端清空本地去重状态后从新窗口回放；
- 若 `after_seq` 太旧，发送新 `room.snapshot` 和最近消息窗口；
- 活动生成使用 `stream.snapshot` 返回当前完整临时文本和最后 `chunk_index`；
- 客户端发现 `room_seq` 缺口时停止追加并请求重新同步；
- 在线人数是快照数据，不参与历史回放。

## 8. 身份、权限与治理

### 8.1 访客身份

匿名访客首次访问时，由服务端生成随机、不可推测的 `visitor_id`，通过 `HttpOnly + Secure + SameSite=Lax` Cookie 维持。显示名是独立、可修改、非唯一字段，不能作为记忆或权限主键。

账号登录后可显式绑定既有匿名身份；绑定操作必须重新认证并留下审计记录。禁止依据显示名、IP、User-Agent 或模型判断自动合并两个人。

### 8.2 角色

第一版最小角色集：

- `guest`：阅读、发言、管理本人偏好；
- `member`：具有可恢复账号和更稳定的个人记忆；
- `moderator`：禁言、删除展示、处理举报；
- `admin`：模型、Persona、记忆、额度和系统配置。

管理员权限不通过公开 WebSocket 指令实现；所有敏感操作走独立管理 API，要求重新认证并记录审计日志。

### 8.3 限流和封禁

至少按以下维度组合限制：IP/网段、`visitor_id`、账号、房间和全局供应商预算。限流先在代理层做粗粒度防护，再在应用层按身份与成本做精确限制。

封禁记录包含作用域、原因、操作者、开始/结束时间和版本。已建立的 WebSocket 在封禁生效后应立即撤销。

## 9. 三层记忆设计

### 9.1 作用域

| 层 | 主体键 | 内容 | 写入者 | 默认可见性 |
|---|---|---|---|---|
| 房间共享记忆 | `room_id` | 房间事件、共同话题、公开约定 | 策略审核后的 Memory pipeline | 房间公开 |
| 访客独立记忆 | `visitor_id` | 偏好、关系、历史互动 | 策略审核后的 Memory pipeline；本人可请求删除 | 仅构造对该访客回复时使用 |
| NEKO Persona | `character_id` | 性格、身份、边界、长期设定 | 管理员 | 进入每次回复，禁止访客改写 |

现有 `master` 中混合的“主人事实”和关系知识不能原样映射到公共房间。迁移时必须拆成 Persona、指定管理员资料或废弃项。

### 9.2 写入门禁

对话完成后先保存不可变来源记录，再异步提取候选事实。候选事实至少包含：

- `source_message_ids`
- `subject_type` 与 `subject_id`
- `speaker_id`
- `scope`：room / visitor
- `visibility`
- `confidence`
- `event_time`
- `status`：pending / active / denied / archived
- `sensitivity`

规则：

1. 对话永远不能产生 `scope=persona` 的自动写入。
2. “大家都认为”“NEKO 就是”等访客陈述仍只是访客声称，不因措辞提升可信度。
3. 单个访客对自己的偏好可进入其个人候选事实，但不能自动成为房间共识。
4. 房间共享事实需要多源证据、管理员批准或明确的安全白名单规则。
5. 密钥、联系方式、精确地址、健康/财务等敏感内容默认不抽取；误存后支持主体删除和审计。
6. 删除展示消息与删除记忆是两个动作；后台必须允许追踪来源并级联处理。

### 9.3 检索与提示词预算

每轮上下文按固定顺序组装：

1. 系统安全规则；
2. 受保护 Persona；
3. 房间行为规则和当前状态；
4. 与目标访客相关且允许公开使用的个人记忆；
5. 相关房间共享记忆；
6. 最近公共消息窗口；
7. 本轮被选中的来源消息。

各段有独立 token 上限，不能让公共聊天历史挤掉 Persona 和安全规则。Memory 返回的内容用“历史资料/不可信陈述”边界包裹，不作为系统指令解释。

访客独立记忆增加可见性分类：

- `public_room_safe`：可用于公共回复；
- `private_hint`：只能影响语气或选择，不应在公共输出中复述；
- `admin_only`：不进入公共模型上下文。

第一版公共房间检索只允许 `public_room_safe`，无法可靠分类时宁可不召回。

### 9.4 审核和遗忘

后台需支持按访客、房间、来源消息和记忆 ID 查询，执行批准、否认、归档、删除和保护。所有管理员变更写审计日志。

访客遗忘至少覆盖：

- 独立记忆及其事实、反思、归档；
- 身份绑定与可识别资料；
- TTS/日志中的可清理派生数据；
- 法律或安全要求允许保留的最小封禁/审计记录应与聊天资料分离，并明确保留期。

## 10. 数据模型

第一版应用数据库建议使用 SQLite WAL，所有 schema 通过迁移管理。核心表如下：

| 表 | 关键字段 | 说明 |
|---|---|---|
| `visitors` | `id`, `account_id?`, `display_name`, `status`, timestamps | 稳定主体 |
| `rooms` | `id`, `slug`, `status`, `last_seq`, config version | 房间权威状态 |
| `messages` | `id`, `room_id`, `room_seq`, `author_type`, `author_id`, `reply_to_id?`, `content`, `status` | 正式时间线 |
| `room_events` | `id`, `room_id`, `room_seq`, `type`, `payload`, `created_at` | 重连回放；可与消息同事务写 |
| `turns` | `id`, `room_id`, `target_visitor_id?`, `source_message_ids`, `reason_code`, `status`, timings | 调度证据链 |
| `client_requests` | `visitor_id`, `request_id`, `result`, `expires_at` | 幂等去重 |
| `bans` | subject/scope, reason, actor, start/end | 权限治理 |
| `jobs` | type, dedupe_key, payload, status, attempts, next_run_at | TTS/记忆可靠后台任务 |
| `audit_log` | actor, action, target, before/after hash, time | 管理操作审计 |

约束：

- `UNIQUE(room_id, room_seq)`；
- `UNIQUE(visitor_id, request_id)`；
- NEKO 正式消息与对应 `room_event` 在同一事务提交；
- `room_seq` 只在持久事件提交时递增，不回收；
- 原始文本与净化后的渲染文本分字段存储，浏览器只接收净化结果；
- 日志不记录 API Key、Cookie、完整提示词和不必要的原始记忆。

## 11. HTTP API 边界

公共 API 最小集合：

- `POST /api/v1/session/guest`：建立/恢复访客会话；
- `GET /api/v1/rooms/{room_id}`：房间基础信息和功能开关；
- `GET /api/v1/rooms/{room_id}/messages?before_seq=`：分页历史；
- `POST /api/v1/ws-ticket`：可选，签发短期连接票据；
- `GET /api/v1/media/{media_id}`：受控读取 TTS 资源；
- `GET /health/live`：进程存活；
- `GET /health/ready`：依赖就绪，只允许代理/监控访问详细信息。

管理 API 使用 `/api/v1/admin/*`。第一版已经覆盖 Persona、记忆审核、封禁、额度、暂停/只读/主动主持控制、取消当前 generation、数据保留/清理和审计查询；模型/TTS 配置仍保留在私有服务端配置目录。不得在同一路由中仅靠前端隐藏按钮区分管理员能力。

## 12. 前端边界

网页只包含：

- Live2D Canvas 与服务器批准的表情/动作映射；
- 公共聊天时间线、回复目标、输入框；
- 个人排队确认和房间总体忙碌状态；
- 在线人数、连接/重连状态；
- 流式临时消息与正式消息替换；
- TTS 播放、静音和浏览器自动播放提示；
- 匿名访客或账号登录；
- 安全 Markdown、移动端布局和基础无障碍支持。

网页不得包含：

- LLM/TTS Key、Memory 服务地址；
- Persona 或原始记忆文件；
- 模型供应商管理配置；
- 管理 API 的静态秘密；
- 主动搭话计时器或“每个浏览器一套”的 Director；
- `eval`、远程脚本动作或模型生成的 HTML。

Markdown 使用严格白名单并禁用原始 HTML；外链添加安全属性；设置 CSP、`frame-ancestors`、资源类型限制和合理的跨域策略。博客嵌入时应显式配置允许的父域，不使用 `*`。

## 13. 故障与降级

| 故障 | 对外行为 | 恢复策略 |
|---|---|---|
| LLM 超时/限流 | `stream.failed`，保留访客消息 | 120 秒总时限；下一条消息重新调用，不自动重复公开回复 |
| Memory 不可用 | 使用 Persona 和最近消息继续 | 读取按下一轮重试；写入默认重试 3 次并标记 memory-degraded |
| TTS 失败 | 文字正常，提示音频不可用 | 默认重试 2 次；达到上限后记录降级状态 |
| WebSocket 断线 | 客户端显示离线并重连 | `after_seq` 补发 + stream snapshot |
| 进程重启 | 活动流中断，正式消息不丢 | 恢复事件、重评未消费消息 |
| 慢客户端 | 断开该连接 | 有界发送队列；不影响其他人 |
| 数据库繁忙/只读 | 暂停接受新消息 | readiness 失败；先恢复写入再开放流量 |
| 内容审核不可用 | 按 fail-closed 策略限制新访客发言 | 管理员可切只读房间 |

任何降级都必须保证：不能把未提交流伪装成正式消息，不能绕过 Persona 写保护，不能因重试重复发言。

## 14. 安全基线

- TLS 终止、HSTS、WSS 和安全 Cookie；
- WebSocket Origin allowlist，拒绝跨站劫持；
- 请求体、帧、文本长度、连接数和速率上限；
- CSRF 防护用于 Cookie 认证的写 HTTP API；
- 管理员 MFA，生产环境优先再叠加 VPN 或 IP allowlist；
- 密钥来自服务端 secret store/环境注入，不写仓库和前端构建；
- 输出净化、CSP、依赖锁定和定期漏洞扫描；
- 用户输入、记忆召回和模型输出分别标记，不允许内容跨越系统指令边界；
- 关闭通用 Agent/工具执行，MemoryFacade 使用固定参数化接口；
- 审计 Persona、记忆、封禁、配置、额度及数据导出/删除；
- 数据库、记忆卷和 TTS 文件分别设置保留期与每日备份；
- 备份加密并定期做恢复演练，不能只验证“备份命令成功”。

## 15. 可观测性和成本控制

结构化日志统一携带 `request_id`, `connection_id`, `room_id`, `turn_id`, `generation_id`，但默认不记录聊天正文。

核心指标：

- 当前连接数、连接/重连失败率；
- 消息接受延迟、队列长度、消息等待时间分位数；
- 首 token 延迟、生成总时长、失败/取消率；
- 每分钟 NEKO 发言数、主动发言互动率、重复度；
- LLM token、TTS 字符/秒数及按日费用；
- Memory 检索/写入延迟、降级次数和待处理任务；
- WebSocket 慢客户端断开数；
- 封禁、限流和审核命中数。

第一版建议告警：

- 5 分钟内 LLM 失败率持续超过阈值；
- 队列最老消息等待超过产品上限；
- 房间出现两个活动 generation；
- `room_seq` 唯一约束冲突；
- Memory/TTS jobs 积压持续增长；
- 每日供应商成本达到软/硬预算。

硬预算触发后进入降级模式：降低主动发言频率、关闭 TTS、限制匿名访客频率，最后切只读；不在无提示的情况下无限消费额度。

## 16. 部署与备份

### 16.1 单 VPS 拓扑

建议容器/进程：

- `proxy`
- `neko-api`
- `memory-service`
- `tts-worker`（可选）
- `backup-job`

仅 `proxy:443` 映射公网端口。其他进程在私有网络监听，管理入口经 proxy 强认证。数据库与 Memory 使用独立持久化卷；静态 Live2D 资源可由 proxy 或对象存储/CDN 提供，但需确认模型和声音的公网授权。

### 16.2 备份

- SQLite 使用在线一致性备份 API，不直接复制正在写入的数据库文件；
- Memory 服务按其原子写入/日志契约制作一致性快照；
- 每日全量 + 更短周期增量或快照；
- 至少保留一份异机/异区加密副本；
- 备份清单记录 schema 版本、应用版本、Persona 版本和校验和；
- 每月至少恢复到隔离环境一次，验证房间序号、消息、Persona、记忆和 TTS 引用一致。

## 17. 现有仓库的目标结构

第一版继续使用现有 Python/FastAPI 工程和目录，只增加房间域并收缩其他目录。裁剪完成后的主要代码边界应接近：

```text
app/
  main_server/           # 精简后的公共 HTTP/WS 入口与内部管理入口
  memory_server/         # 保留现有长期记忆服务，修改主体/作用域接口
main_logic/
  core/                  # 保留文本 LLM、流式解析、必要 TTS；移除 ASR/工具/桌面状态
  room/                  # 新增 Hub、Queue、Director、room actor、事件与身份
main_routers/
  public_room_router.py  # 公共页面所需只读 HTTP API
  room_websocket_router.py
  admin_router/          # 私有配置、Persona、记忆、封禁、额度、日志 CRUD
  live2d_router.py       # 只保留运行时读取能力
memory/                  # 保留 Facts/Reflections/Persona/Recall，增加房间/访客作用域
config/                  # 只保留模型、Memory、TTS、Live2D、房间和安全配置
templates/ or frontend/  # 只保留一套公共网页构建来源与一套管理后台来源
static/                  # 仅网页运行资源和许可明确的 Live2D 资产
docs/
deploy/
migrations/
```

最终仓库不应继续出现仅服务于 Agent、插件、Steam、游戏、桌面启动、ASR、VRM、MMD 或 PNGTuber 的顶级目录。通用工具若仍被保留链路使用，应移动或改名到保留域，避免用已删除功能的目录充当隐式依赖。

WebSocket schema 应成为单一事实源，并生成或校验前后端类型，避免继续在大型路由文件和前端脚本中各自手写协议。

## 18. 现有代码增删改查清单

### 18.1 保留并原地修改

| 当前落点 | 操作 | 第一版结果 |
|---|---|---|
| `app/main_server/web_app.py` | 改 | 从“导入并挂载全部路由”改成明确 allowlist；删除 market bridge、桌面 shutdown、卡片等专用端点 |
| `app/memory_server/`、`memory/` | 保留并改 | 继续使用现有记忆流水线；增加 room/visitor/persona 作用域和写入门禁，服务仅内网可达 |
| `main_logic/core/manager.py` 及文本生成代码 | 拆改 | 保留模型 provider、提示词和流式解析；去掉单一 `self.websocket`、`master_name`、ASR、截图、工具和前端语音租约耦合 |
| 现有 TTS pipeline | 改 | 从“向当前 socket 推音频”改成每条 NEKO 消息生成一次、缓存一次、广播同一结果 |
| `main_routers/live2d_router.py` 与必要 Live2D 资源 | 收窄 | 只提供公共页面读取模型/表情所需接口；编辑配置进入私有后台 |
| `templates/index.html` 及其直接依赖 | 大幅删改 | 保留 Live2D、时间线、输入、队列、在线人数、流式文字、TTS 和重连；删除其他渲染器、ASR、游戏与前端主动搭话 |
| `config/` 中模型 provider 配置 | 收窄 | 保留文本 LLM、Memory、TTS、Live2D、房间和安全配置，密钥只在服务端 |

`LLMSessionManager` 当前同时混入 TTS、ASR、主动搭话、工具、截图和单 socket 状态。第一版不应继续把完整 manager 当作多人房间核心；应保留其已经工作的模型/流式小模块，把传输改为回调或事件 sink，再删除不需要的 mixin 和实例字段。

### 18.2 新增

| 新能力 | 建议落点 |
|---|---|
| RoomConnectionHub、RoomTurnQueue、RoomDirector、AvatarState | `main_logic/room/` |
| 房间 actor、`room_seq`、幂等、事件回放和后台 jobs | `main_logic/room/` + 应用数据库迁移 |
| 多人 WebSocket v1 | `main_routers/room_websocket_router.py` |
| 匿名访客/账号身份、封禁、限流 | room identity/auth 模块与公共 session API |
| Persona、记忆、配置、Live2D、封禁、额度、日志 CRUD | `main_routers/admin_router/` + 独立管理页面 |
| 协议 schema 与错误码 | 保留仓库内的独立 protocol 模块/文档 |

### 18.3 提取后删除来源

| 来源 | 只提取 | 随后删除 |
|---|---|---|
| `plugin/plugins/neko_live/` | 节奏、目标选择、重复抑制、冷场 reason code | 直播平台、礼物、SC、插件生命周期以及最终整个插件目录 |
| 当前 `main_routers/websocket_router.py` | 可复用的文本模型启动、流式事件和错误处理 | 最新连接覆盖、语音输入、桌面消息、工具、截图及旧协议路由 |
| 当前前端主动搭话实现 | reason code/展示文案中确有价值的部分 | 所有浏览器定时器和本地主持状态 |
| 角色/配置路由 | Live2D、Persona、模型/TTS 必要字段校验 | 卡片、Workshop、其他渲染器和本地桌面配置面 |

新房间入口完成切换后，旧 WebSocket 路由必须实际删除，不能用永久 feature flag 留在生产包中。

### 18.4 整体删除

完成引用审计后删除以下能力及其专用依赖、配置、文档入口、构建脚本和静态资产：

- `app/agent_server/`、Agent/OpenFang、浏览器和电脑控制；
- `plugin/`、插件市场、插件管理前端和 Market bridge；
- `steamworks/`、Workshop、云存档、桌面启动器及打包发布代码；
- VRM、MMD、PNGTuber、VMC 的路由、前端、模型和演示资源；
- ASR、麦克风、实时语音输入、语音克隆和声音公开编辑；
- 游戏、Galgame、卡片、截图/OCR、桌面小组件、教程、成就和音乐点播；
- Bilibili/Twitch/抖音、礼物/SC/舰长接入；
- 重复前端构建产物、未被最终公共页面或后台引用的依赖和资产。

删除顺序是：停止挂载 → 删除调用 → 删除实现 → 删除依赖/配置 → 删除资产/文档 → 做全仓引用和产物体积检查。仅停止挂载不算完成。

### 18.5 后台查与管

| 对象 | 查询 | 新增/修改/删除规则 |
|---|---|---|
| Persona | 查看版本、来源、当前生效内容 | 仅 admin；版本化保存；支持回滚；禁止自动写入 |
| 访客/房间记忆 | 按主体、作用域、来源和状态查询 | 审批、否认、归档、删除均审计；支持访客遗忘 |
| 模型/TTS 配置 | 查看脱敏配置、连通性和用量 | 密钥只写不回显；修改需要重新认证 |
| Live2D | 查询当前模型、表情映射和许可信息 | 只允许白名单资源；修改产生配置版本 |
| 访客与封禁 | 查询身份、限流、处分和申诉记录 | moderator/admin 分权；生效后撤销现有连接 |
| 房间 | 查询状态、队列、当前 turn 和预算 | 暂停、只读、关闭主动主持、取消 generation |
| 消息/日志 | 按序号、turn、错误码查询 | 消息采用审核/软删除状态；日志遵守脱敏和保留期 |

## 19. 交付阶段

### Phase 0：收窄现有入口

- 固定当前可工作的文本 LLM、Memory、Live2D、TTS 基线；
- 给 `web_app.py` 建公共产品路由 allowlist，未列出的旧路由不再导入和挂载；
- 定义协议 schema、错误码、数据库迁移、配置和 secret 规范；
- 建立删除台账：每个待删目录记录剩余 import、路由、静态引用和依赖。

验收：现有底座仍可启动，但公网入口只能访问公共产品所需路由；无关功能不可达。

### Phase 1：替换单连接文字链路

- 在现有 Main Server 内增加 Hub、Queue、room actor 和多人 WebSocket；
- 从现有 manager 复用文本 provider 与流式解析，改为 transport-neutral event sink；
- 实现身份、消息提交、`room_seq`、幂等、回放和单一 generation；
- 改造现有首页，只保留公共时间线和文字输入的最小链路。

验收：两个浏览器同时连接、发言、断线重连，时间线无重复、无乱序，始终只有一个 NEKO generation。

### Phase 2：公共房间调度与记忆

- 从 NEKO Live 提取目标选择、公平性、冷却和重复抑制后删除插件耦合；
- 增加服务器唯一 RoomDirector 和 reason code；
- 把现有 Memory 改为房间、访客、Persona 三层；
- 将 `master_name` 单主体提示词改为当前目标访客与公共房间上下文。

验收：30 分钟 10–50 人模拟房间中不抢话、不连续刷屏、不泄露 A 的非公开记忆，访客不能改变 Persona。

### Phase 3：Live2D、TTS 与管理 CRUD

- 收窄现有 Live2D 加载和情绪映射；
- 把现有 TTS 改成一次生成、多端共享；
- 建立 Persona、记忆、模型、Live2D、封禁、额度和日志后台；
- 增加暂停、只读、关闭主动主持和取消 generation 的运营开关。

验收：多客户端的文字、表情和 TTS 一致；管理操作有权限、有版本、有审计。

### Phase 4：物理裁剪与公网验收

- 按 §18.4 删除不需要的模块、依赖、配置、模板、资产和文档入口；
- 删除旧 WebSocket 和旧首页链路，不保留永久 legacy 模式；
- 检查全仓无残余 import、路由、环境变量、构建入口和静态引用；
- 完成限流、CSP、日志脱敏、预算、告警、备份和恢复演练。

验收：最终代码树只含第一版所需功能；单 VPS 在目标并发下稳定运行，关闭 Memory/TTS/LLM 任一依赖均按设计降级。

## 20. 上线门槛

功能：

- 幂等发送、严格房间顺序、断线续传、活动流快照均通过；
- 排队、目标访客、流式文字、表情和 TTS 在多客户端一致；
- 管理后台可暂停房间、封禁访客、审核记忆、回滚 Persona；
- RoomDirector 的每次主动发言可解释且有频率上限。

安全与隐私：

- 无密钥、Memory 地址或管理能力进入前端包；
- WS Origin、CSRF、XSS、Markdown、速率、消息大小与权限边界完成检查；
- Persona 自动写入路径为零；
- 个人记忆隔离和遗忘流程完成验证；
- Live2D、字体、音色及其他公开资产许可已确认。

运维：

- 生产只有 443 暴露；
- 监控、预算硬限制、日志脱敏和告警启用；
- 备份在隔离环境成功恢复；
- 数据库 schema、Persona 与协议版本均可追踪；
- 有只读、停用主动主持和完全下线的应急开关。

## 21. 尚未完成的产品决策

以下问题不改变本文总体架构，但必须在对应功能进入公网前形成简短 ADR：

1. 账号登录供应商和管理员 MFA 方案；
2. 内容审核供应商、失败时 fail-closed 的具体范围；
3. Live2D 模型、动作、字体和 TTS 音色的公网使用许可；
4. 博客最终使用链接、同域反代还是受限 iframe 嵌入。

已完成的边界决策由[数据保留 ADR](./retention-policy.md)和[公网边界与依赖降级 ADR](./public-edge-security.md)承接。未完成上述决策前，不得开放账号、公开未审核音色、接入内容审核或放宽 iframe 策略。
