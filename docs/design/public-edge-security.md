# ADR：公网边界与依赖降级基线

> 状态：已接受并实现
>
> 日期：2026-08-15
>
> 适用版本：v0.1+

## 决策

NEKO-ONE 第一版只允许现有 1Panel OpenResty 暴露 TCP 443。公共 FastAPI 与 Memory 服务必须监听回环地址，LLM/TTS Key 继续保存在服务端私有配置中。

Memory 是公共房间的弱依赖：systemd 使用 `Wants` 而不是 `Requires`。Memory 停止时，公共消息、Persona 和最近消息仍可继续工作，不能连带停止房间进程。

## 入口限制

| 边界 | 默认值 | 执行位置 |
| --- | ---: | --- |
| HTTP 请求体 | 32 KiB | OpenResty 与 FastAPI `Content-Length` 边界 |
| WebSocket 单帧 | 8192 字符、16384 字节 | 应用协议与 Uvicorn |
| 同 IP HTTP 连接 | 20 | OpenResty |
| 同 IP WebSocket | 3 | OpenResty |
| 游客会话创建 | 5 次/分钟，burst 3 | OpenResty |
| 管理入口 | 60 次/分钟，burst 20 | OpenResty；应用另有登录限速 |
| 一般 HTTP | 30 次/秒，burst 60 | OpenResty |
| LLM 单轮总时限 | 120 秒 | `PublicRoomService` |

OpenResty 对超出速率或连接数的请求返回 429。应用仍保留访客级消息长度、窗口频率、WebSocket Origin、Cookie 身份和幂等校验；代理限制不能替代业务限制。

详细 readiness 只允许同机监控或负载均衡器从回环地址访问，OpenResty 对公网请求返回 403；公开 liveness 只证明进程存活，不返回依赖状态。readiness 会执行 SQLite `quick_check`、最长 2 秒等待的可回滚写入，并检查最低磁盘余量、主房间及 LLM/Persona 配置。响应只公开布尔状态和错误类别，不返回磁盘容量、模型地址、Memory 地址或凭据。

## 浏览器安全策略

应用直连响应与 OpenResty 公网响应都设置以下基线：

- CSP 只允许同源脚本、样式、字体、媒体和连接；禁止 `object`、`base` 与任意父页面；
- HSTS、`nosniff`、`DENY`、same-origin Referrer/COOP/CORP；
- 禁止摄像头、麦克风、定位、支付和 USB 权限；
- 管理页面与管理 API 使用 `Cache-Control: no-store`；
- 管理 Cookie 为 `HttpOnly`、`SameSite=Strict`，游客 Cookie 为 `HttpOnly`、`SameSite=Lax`，公网必须启用 `Secure`。

第一版由 `pardofelis-web` 普通链接进入 `https://neko.pardofelis.wiki/`，默认不允许 iframe。未来确需嵌入时，必须同时把应用和 OpenResty 的 `frame-ancestors 'none'` 改为精确的 `https://pardofelis.wiki`；禁止使用 `*`，并重新验证 Cookie 与点击劫持边界。

## 依赖故障语义

| 依赖 | 故障行为 | 自动恢复 |
| --- | --- | --- |
| LLM | `stream.failed`；访客消息保留，不创建伪造的助手消息 | 不自动重放公开回复；下一条排队消息重新调用，单轮超过 120 秒取消 |
| Memory 读取 | 提示词标记 `memory-degraded`，仅使用 Persona 与最近公共消息 | 下一轮重新读取 |
| Memory 写入 | 已提交文字不回滚 | 默认最多 3 次指数间隔重试；最终失败记录降级状态 |
| TTS | 已提交文字不回滚，广播 `speech.failed` | 默认最多 2 次重试；下一轮再次调用 |

只有 SQLite 完整性/可写性/磁盘余量、主房间存在以及 LLM/Persona 配置属于阻断就绪的核心条件。Memory、TTS 与 Live2D 是可降级能力：它们会出现在详细探针中，但其单独故障不会让纯文本公共房间退出服务。

管理后台只显示 `ready`、`degraded`、`disabled` 或 `unknown`、错误类型和连续失败次数，不返回供应商响应、Key、Memory 地址或聊天正文。

这些重试都发生在正式文字提交之后或之外，绝不能再次提交助手消息。第一版的 Memory 写入重试不是跨进程持久 outbox；进程重启后的可靠补写属于公网 Beta 的数据可靠性工作。

## 验证与上线门槛

本地可重复验证：

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_public_room.py
uv --cache-dir .uv-cache run --locked python scripts/check_public_boundary.py
uv --cache-dir .uv-cache run --locked python scripts/verify_deployment_security.py
```

`verify_public_room.py` 会故意触发 LLM 超时、Memory 连接/写入故障和 TTS 连续失败，因此日志中出现对应异常是预期行为；脚本还必须证明后续调用恢复且房间进程未退出。

静态验证不能替代目标 VPS 上的解析和网络检查。上线前必须执行 `nginx -t`、确认只有 443 对公网监听，并从外部验证 HSTS/CSP、429、413、Origin 拒绝与管理员网络隔离。
