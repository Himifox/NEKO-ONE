<div align="center">

# NEKO-ONE

### 它记得你，也记得这个房间。

拥有 Live2D 身体、连续人格和分层长期记忆的公共房间主持者。

![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![WebSocket](https://img.shields.io/badge/WebSocket-Realtime-6A5ACD)
![License](https://img.shields.io/badge/License-Apache%202.0-blue)

[快速开始](#快速开始) · [它有什么不同](#它有什么不同) · [架构](#架构) · [文档](docs/design/index.md) · [路线图](docs/roadmap.md)

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
| **房间会感知时间** | 早晨、中午、下午、晚上与深夜五套氛围背景自动切换 |
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

第一版采用单实例、单主写入者和 PostgreSQL，让消息顺序、模型输出、记忆写入和 TTS 广播保持确定。

## 快速开始

### 1. 准备环境

需要：Python **3.11**、[uv](https://docs.astral.sh/uv/)、PostgreSQL **15+**、已配置的私有 LLM 参数。

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

真实 LLM/TTS 密钥保存在 NEKO 的私有服务端配置目录中，**不要**写入 `.env.public.example`、前端文件或 Git。

### 2. 启动两个服务

```powershell
uv run --env-file .env.public neko-memory   # 终端一：私有记忆服务
uv run --env-file .env.public neko-public   # 终端二：公共房间
```

打开：

- 公共房间：<http://127.0.0.1:48911/>
- 管理后台：<http://127.0.0.1:48911/admin>
- 就绪检查：<http://127.0.0.1:48911/api/v1/health/ready>

如果 `NEKO_PUBLIC_ADMIN_PASSWORD` 少于 12 位或未设置，管理 API 会保持禁用。就绪检查会验证 PostgreSQL 连接、schema 版本、可回滚写入、磁盘余量和 LLM/Persona 配置；Memory、TTS 与 Live2D 作为可降级能力单独报告。

## 记忆与身份边界

记忆拆成三层，而不是把所有访客的话混进一个向量库：

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

完整机制见[长期记忆架构](docs/architecture/memory-system.md)和[数据保留策略](docs/design/retention-policy.md)。

## 管理后台

`/admin` 是服务器管理入口。支持查看房间、访客、消息与审计状态，编辑 Persona，审核共享记忆，封禁访客，隐藏消息，切换 Live2D 形象，调整限额与保留策略，以及查看 LLM、Memory、TTS 的降级状态。生产入口叠加 OpenResty Basic Auth、签名 `HttpOnly` Cookie 和 CSRF 校验，详见[部署说明](deploy/README.md)。

## 部署到公网

推荐拓扑是一台 VPS 上的两个回环服务加一个公开反向代理：

| 网络入口 | 作用 | 是否公开 |
| --- | --- | --- |
| `443/tcp` | 1Panel OpenResty HTTPS / WSS | **是** |
| `127.0.0.1:48911` | 公共房间 API | 否 |
| `127.0.0.1:5432` | PostgreSQL | 否 |
| Memory Server 端口 | 长期记忆服务 | 否 |
| LLM / TTS 配置 | 供应商凭据 | 否 |

仓库提供 OpenResty/Nginx 配置、systemd unit 和环境变量模板，边缘已包含 CSP、HSTS、请求体、速率及连接上限。参见[部署说明](deploy/README.md)和[公网边界设计](docs/design/public-edge-security.md)。

## 验证

`scripts/` 下提供确定性验证脚本，用假模型/记忆/TTS 覆盖多人连接、消息序号、幂等发送、断线续传、管理审核、封禁、限额、应急控制、数据清理、降级与恢复，以及容量、备份恢复、资产清单和安全边界。真实供应商不在普通回归中调用。完整命令与门槛见[容量与稳定性手册](docs/operations/capacity-and-soak.md)、[备份恢复手册](docs/operations/backup-and-restore.md)和[真实供应商验收手册](docs/operations/provider-acceptance.md)。

## 仓库边界

### 保留

文本 LLM、多供应商适配、流式输出、Live2D、情绪标签、TTS、分层记忆、Persona、公共房间协议和私有管理后台。

### 明确不包含

Agent Server、插件市场、浏览器/电脑控制、Steam、云存档、游戏、Galgame、ASR、实时语音输入、VRM、MMD、PNGTuber、桌面启动器及直播平台礼物逻辑。

## 当前版本

`v0.1` 是可部署的单房间基线，设计目标为 **10–50 人的小型公共房间**。当前使用游客身份、单实例和 PostgreSQL，不依赖 Redis；账号体系、多实例协调、直播平台接入以及公开声音配置不在第一版范围内。后续阶段见[产品路线图](docs/roadmap.md)。

## 项目结构

```text
app/public_room_server/   公共 FastAPI 入口
app/memory_server/        私有长期记忆服务
main_logic/room/          连接、队列、导演、对话、记忆与语音编排
main_routers/             公共 HTTP、管理 API 与 WebSocket 协议
frontend/public-room/     访客页面
frontend/public-admin/    私有管理后台
deploy/                   OpenResty/Nginx 与 systemd 部署配置
docs/                     架构、设计与运维文档
```

## License

本项目采用 [Apache License 2.0](LICENSE)。第三方资源的授权信息见 [NOTICE](NOTICE)。
