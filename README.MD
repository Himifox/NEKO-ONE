<div align="center">

# NEKO-ONE

### 它记得你，也记得这个房间。

拥有 Live2D 身体、连续人格和分层长期记忆的公共房间主持者。

![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![WebSocket](https://img.shields.io/badge/WebSocket-Realtime-6A5ACD)
![License](https://img.shields.io/badge/License-Apache%202.0-blue)

[快速开始](#快速开始) · [它有什么不同](#它有什么不同) · [架构](#架构) · [路线图](docs/roadmap.md) · [部署](#部署到公网) · [设计文档](docs/design/public-room-web-architecture.md)

</div>

---

NEKO-ONE 不是把大模型塞进聊天框，也不是每次刷新后重新认识你的虚拟角色。

它是一个长期在线的公共房间实体：看得见访客、知道该回复谁、能控制房间节奏，并把对话沿着 **Recent → Facts → Reflections → Persona** 的证据链沉淀下来。文字、表情和声音由服务器统一生成，因此所有人看到的是同一个 NEKO，而不是各自浏览器里的十个副本。

> **一句话定位：** 一个拥有身体、连续记忆，能辨认不同访客并主持公共房间的长期在线实体。

## 它有什么不同

| 能力 | NEKO-ONE 的做法 |
| --- | --- |
| **统一人格** | Persona 与反思长期存在，不随网页刷新消失 |
| **有证据的记忆** | 保留原始时间记录、事实去重、反思归档与混合召回 |
| **认识不同访客** | 房间共享记忆与访客独立记忆严格分域，避免串话 |
| **会主持房间** | 唯一的服务器端 `RoomDirector` 选择回复对象、控制频率并避免刷屏 |
| **有身体和声音** | Live2D 表情跟随流式回复，一份 TTS 音频广播给全房间 |
| **为公网而收敛** | 浏览器只获得公开消息与渲染资源，模型密钥、Persona 和原始记忆始终留在服务端 |

### 房间里的体验

- 多访客实时聊天、在线人数和回复队列状态；
- LLM 流式文字、情绪标签与 Live2D 动作同步；
- 同一条回复只合成一次语音，并共享给所有在线访客；
- WebSocket 自动重连，按房间序号续传错过的事件；
- `request_id` 幂等发送，网络抖动不会重复写入消息；
- 管理员可审核记忆、保护 Persona、隐藏消息、封禁访客和调整限额。

公开仓库不附带来历不明的角色模型或默认音色。运营方把自有授权的 Cubism 4 模型安装到私有数据目录后即可启用 Live2D；未配置时聊天、记忆和 TTS 服务保持可用并显示安全占位。参见[公网资产授权手册](docs/operations/public-assets.md)。

## 架构

```mermaid
flowchart LR
    V["访客浏览器"] -->|"HTTPS / WSS"| RP["1Panel OpenResty"]
    A["私有管理后台"] -->|"HTTPS"| RP
    RP --> API["neko-public :48911"]

    subgraph PublicRuntime["公共房间运行时"]
        HUB["RoomConnectionHub"]
        QUEUE["RoomTurnQueue"]
        DIRECTOR["RoomDirector"]
        ENGINE["ConversationEngine"]
        AVATAR["AvatarState"]
        SPEECH["SpeechService"]
    end

    API --> HUB
    HUB --> QUEUE --> DIRECTOR --> ENGINE
    ENGINE --> AVATAR
    ENGINE --> SPEECH
    API -->|"业务数据"| POSTGRES["PostgreSQL :5432"]
    ENGINE -->|"仅回环地址"| MEMORY["Memory Server"]
    ENGINE -->|"服务端密钥"| LLM["LLM Provider"]
    SPEECH -->|"服务端密钥"| TTS["TTS Provider"]
```

第一版采用单实例、单主写入者和 PostgreSQL。这个约束让消息顺序、模型输出、记忆写入和 TTS 广播保持确定；需要水平扩容时，再引入 Redis、fencing token 与对象存储。

## 快速开始

### 1. 准备环境

需要：

- Python **3.11**；
- [uv](https://docs.astral.sh/uv/)；
- PostgreSQL **15+**（服务端与 `pg_dump` / `pg_restore` 客户端工具）；
- 已配置的私有 LLM 参数；
- 可选的 TTS 参数。

```powershell
git clone https://github.com/Himifox/NEKO-ONE.git
Set-Location NEKO-ONE
uv sync --locked
Copy-Item .env.public.example .env.public
```

编辑 `.env.public`，至少替换以下值：

```dotenv
NEKO_PUBLIC_ALLOWED_ORIGINS=http://127.0.0.1:48911
NEKO_PUBLIC_DATABASE_URL=postgresql://neko_public:replace-me@127.0.0.1:5432/neko_public
NEKO_PUBLIC_SESSION_SECRET=replace-with-at-least-48-random-characters
NEKO_PUBLIC_ADMIN_PASSWORD=replace-with-a-long-unique-admin-password
```

真实 LLM/TTS 密钥继续保存在 NEKO 的私有服务端配置目录中，**不要**写入 `.env.public.example`、前端文件或 Git。

### 2. 启动两个服务

终端一——启动私有记忆服务：

```powershell
uv run --env-file .env.public neko-memory
```

终端二——启动公共房间：

```powershell
uv run --env-file .env.public neko-public
```

打开：

- 公共房间：<http://127.0.0.1:48911/>
- 管理后台：<http://127.0.0.1:48911/admin>
- 存活检查：<http://127.0.0.1:48911/api/v1/health/live>
- 就绪检查：<http://127.0.0.1:48911/api/v1/health/ready>

如果 `NEKO_PUBLIC_ADMIN_PASSWORD` 少于 12 位或未设置，管理 API 会保持禁用。
就绪检查会实际验证 PostgreSQL 连接、schema 版本、可回滚写入、最低磁盘余量和 LLM/Persona
配置；Memory、TTS 与 Live2D 作为可降级能力单独报告。生产 OpenResty 只允许
本机监控访问详细就绪结果，公网只保留不含依赖细节的存活检查。

## 记忆与身份边界

NEKO-ONE 把记忆拆成三层，而不是把所有访客的话混进一个向量库：

```text
管理员保护的 Persona
        ↑ 仅管理员可以修改
房间共享记忆
        ↑ 仅审核后的事实可以写入
访客独立记忆
        ↑ 只记录该访客与 NEKO 的交互
```

- 普通访客不能修改 Persona；
- 单个访客的陈述不会自动升级为房间共享事实；
- 当前回复只召回房间记忆与目标访客自己的记忆；
- 管理员可以审核公共事实，也可以彻底遗忘指定访客；
- 浏览器不会收到原始记忆文件、内部主体 ID 或 Memory Server 地址。

默认保留期为消息/事件 30 天、匿名访客 90 天、审计 180 天、共享语音 24 小时。清理访客时必须先成功遗忘其 Memory；失败会保留本地身份并在下一轮重试。旧事件窗口已过期时，服务端用 `replay.reset` 明确要求客户端重建时间线。

完整机制见 [长期记忆架构](docs/architecture/memory-system.md)和[数据保留 ADR](docs/design/retention-policy.md)。

## 管理后台

`/admin` 是服务器管理入口，不是公开的模型配置页面。当前支持：

- 查看房间、访客、消息和审计状态；
- 编辑受保护的角色 Persona；
- 添加审核后的房间共享事实；
- 删除指定访客的独立记忆；
- 封禁或恢复访客；
- 隐藏或恢复公共消息；
- 查看已安装的 Live2D 模型，安全切换当前形象或停用 Live2D；
- 调整消息长度、频率和窗口限额。
- 暂停房间、切换只读、关闭主动主持或取消当前回复。
- 设置数据保留期、查看上次清理结果或立即执行清理。
- 查看 LLM、Memory 与 TTS 最近一次调用的就绪/降级状态。

生产入口先要求一组独立的 OpenResty Basic Auth 凭据，再使用应用管理员密码、
签名的 `HttpOnly` Cookie 和修改操作 CSRF 校验。两组密码不得复用；生产环境必须
启用 HTTPS 和安全 Cookie。具体凭据文件创建和权限见[部署说明](deploy/README.md)。

## 部署到公网

推荐拓扑是一台 VPS 上的两个回环服务加一个公开反向代理：

| 网络入口 | 作用 | 是否公开 |
| --- | --- | --- |
| `443/tcp` | 现有 1Panel OpenResty HTTPS / WSS | **是** |
| `127.0.0.1:48911` | 公共房间 API | 否 |
| `127.0.0.1:5432` | PostgreSQL 公共业务数据库 | 否 |
| Memory Server 端口 | 长期记忆服务 | 否 |
| LLM / TTS 配置 | 供应商凭据 | 否 |

实际宿主网站是 [`Himifox/pardofelis-web`](https://github.com/Himifox/pardofelis-web)：`pardofelis.wiki` 保持静态站，NEKO 使用独立的 `https://neko.pardofelis.wiki/` 子域并由现有 1Panel OpenResty 反代到回环服务。第一版从主站使用普通链接进入，不使用 iframe，也不把 NEKO 文件放进 Vite `dist/`。完整边界见 [pardofelis-web 集成 ADR](docs/design/pardofelis-web-integration.md)。

仓库提供了 OpenResty/Nginx 配置、两个 systemd unit 和环境变量模板。边缘配置已包含 CSP、HSTS、请求体、速率及连接上限，并用独立 Basic Auth 隔离管理入口；应用与 Uvicorn 还会限制 HTTP 请求体和 WebSocket 帧。Memory 使用弱依赖，服务不可用时房间退化为 Persona 与最近消息，不会随之退出。参见[部署说明](deploy/README.md)和[公网边界 ADR](docs/design/public-edge-security.md)。上线前请替换所有密钥，并为公共数据库、记忆与私有配置建立每日备份和恢复演练。

## 验证

所有 Python 验证均通过 `uv` 运行：

以下数据库验证会永久清空目标库。只能使用名称包含 `test`、`verify` 或 `ci` 的
专用数据库，并显式设置 `NEKO_VERIFY_ALLOW_DATABASE_RESET=1`；严禁使用生产 DSN。
备份恢复验证还需要一个独立的空恢复库。完整 Debian 命令见
[部署说明](deploy/README.md)。

```powershell
$env:NEKO_PUBLIC_DATABASE_URL = "postgresql://neko_verify:...@127.0.0.1:5432/neko_verify"
$env:NEKO_POSTGRES_RESTORE_URL = "postgresql://neko_verify:...@127.0.0.1:5432/neko_restore_verify"
$env:NEKO_VERIFY_ALLOW_DATABASE_RESET = "1"
uv run --locked python scripts/verify_public_room.py
uv run --locked python scripts/verify_postgres_migration.py
uv run --locked python scripts/check_public_boundary.py
uv run --locked python scripts/verify_deployment_security.py
uv run --locked python scripts/verify_room_capacity.py
uv run --locked python scripts/verify_backup_restore.py
uv run --locked python scripts/verify_public_assets.py
uv run --locked python scripts/verify_provider_acceptance.py
node --check frontend/public-room/app.js
node --check frontend/public-room/live2d.js
node --check frontend/public-admin/app.js
```

`verify_public_room.py` 使用假的模型、记忆和 TTS 实现，不会调用真实供应商或污染正式记忆。它覆盖多人连接、消息序号、幂等发送、断线续传、管理审核、封禁、限额、应急控制、数据清理、过期回放重置，以及 LLM 超时、Memory 故障和 TTS 失败后的降级与恢复。`verify_room_capacity.py` 进一步验证 10/25/50 人并发、严格事件顺序、单 generation、慢客户端隔离和任务回收；长时命令及门槛见[容量与稳定性运维手册](docs/operations/capacity-and-soak.md)。`verify_backup_restore.py` 覆盖真实 `pg_dump`/`pg_restore`、Memory SQLite 在线备份、清单校验、隔离恢复、损坏和路径逃逸拒绝；生产加密与异机演练见[备份恢复手册](docs/operations/backup-and-restore.md)。

`verify_memory_runtime.py` 会在隔离目录启动真实 Memory 应用，验证健康指纹、
旧存储控制路由已消失，并确认不会重新创建任何云同步目录或状态文件。

`verify_public_assets.py` 对浏览器运行库逐文件核对版本、SHA-256 和许可证，并阻止无授权模型、声音或字体重新进入公开发行物。

真实供应商不会在普通回归中调用。上线前先执行无费用预检，再通过显式费用确认运行一次隔离的 LLM、Memory、TTS 验收，详见[真实供应商验收手册](docs/operations/provider-acceptance.md)。

## 仓库边界

### 保留

文本 LLM、多供应商适配、流式输出、Live2D、情绪标签、TTS、分层记忆、Persona、公共房间协议和私有管理后台。

### 明确不包含

Agent Server、插件市场、浏览器/电脑控制、Steam、云存档、游戏、Galgame、ASR、实时语音输入、VRM、MMD、PNGTuber、桌面启动器及直播平台礼物逻辑。

发行边界检查还会拒绝桌面截图、游戏状态、教程、直播/网页抓取、音乐点播、
Workshop、Steam 云包、声音克隆编辑器及对应提示词重新进入源码树和公开发行物。
长期记忆继续写入本地持久化目录；旧云同步、快照导入导出、存储迁移闸门和
远端恢复实现已经移除，生产备份统一使用可校验的 `scripts/manage_backup.py` 流程。

## 当前版本

`v0.1` 是可部署的单房间基线，设计目标为 **10–50 人的小型公共房间**。当前使用游客身份、单实例和 PostgreSQL，不依赖 Redis；账号体系、多实例协调、直播平台接入以及公开声音配置不在第一版范围内。

后续阶段、进入条件和验收门槛见 [产品路线图](docs/roadmap.md)。当前优先完成阶段 1 的真实供应商、公网安全、容量和备份恢复验收，再进入阶段 2。

## 项目结构

```text
app/public_room_server/   公共 FastAPI 入口
app/memory_server/        私有长期记忆服务
main_logic/room/          连接、队列、导演、对话、记忆与语音编排
main_routers/             公共 HTTP、管理 API 与 WebSocket 协议
frontend/public-room/     访客页面
frontend/public-admin/    私有管理后台
deploy/                   OpenResty/Nginx 与 systemd 部署配置
docs/                     当前架构与设计文档
```

## License

本项目采用 [Apache License 2.0](LICENSE)。第三方资源的授权信息见 [NOTICE](NOTICE)。
