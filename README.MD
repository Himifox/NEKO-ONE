# NEKO ONE

NEKO ONE 是一个拥有 Live2D 身体、连续人格和分层长期记忆，能够辨认不同访客并主持公共房间的长期在线实体。

这个仓库只保留公共聊天产品所需的能力：

- 多访客 WebSocket、严格消息序号、幂等发送和断线续传；
- 单写入者发言队列与服务器唯一 `RoomDirector`；
- 多供应商文本 LLM 与流式输出；
- 房间共享记忆、访客独立记忆和管理员保护 Persona；
- Live2D 情绪动作与全房间共享 TTS；
- 私有管理后台：Persona、记忆、封禁、消息审核、额度和审计。

不包含 Agent、插件市场、浏览器/电脑控制、Steam、游戏、ASR、VRM、MMD、PNGTuber 或桌面启动器。

## 本地运行

需要 Python 3.11 和 `uv`：

```powershell
uv venv --python 3.11
uv pip install -r requirements-public.txt
Copy-Item .env.public.example .env.public
```

设置环境变量和现有私有模型配置后，分别启动：

```powershell
uv run python -m app.memory_server
uv run python -m app.public_room_server
```

公共页面默认位于 `http://127.0.0.1:48911/`，管理后台位于 `/admin`。如果没有设置至少 12 位的 `NEKO_PUBLIC_ADMIN_PASSWORD`，管理 API 会保持禁用。

## 验证

验证脚本不调用真实模型、TTS 或记忆写入：

```powershell
uv run python scripts/verify_public_room.py
uv run python scripts/check_public_boundary.py
node --check frontend/public-room/app.js
node --check frontend/public-room/live2d.js
node --check frontend/public-admin/app.js
```

## 部署

公网只开放反向代理的 TCP 443。公共服务与 Memory Server 都绑定回环地址，Memory、TTS 上游凭据和管理密钥不得暴露。

参见 [部署说明](deploy/README.md) 和 [架构设计](docs/design/public-room-web-architecture.md)。

## 数据边界

- 访客发言只自动写入其独立记忆；
- 房间共享事实必须由管理员审核后写入；
- Persona 只有管理后台可以修改；
- 浏览器永远不会收到 LLM/TTS 密钥、原始记忆文件或 Memory Server 地址。

## License

Apache License 2.0，详见 [LICENSE](LICENSE)。
