# ADR：公共房间数据保留与自动清理

> 状态：已接受并实现
>
> 日期：2026-08-15
>
> 适用版本：v0.1+

## 决策

NEKO-ONE 默认启用有限期保留，不允许公共消息、匿名访客记忆、审计日志和共享语音无限增长。

| 数据 | 默认期限 | 可配置范围 | 清理行为 |
| --- | ---: | ---: | --- |
| 公共消息、房间事件、turn、幂等请求 | 30 天 | 1–3650 天 | 同一 SQLite 事务删除，房间 `last_seq` 不回收 |
| 匿名访客及其独立记忆 | 90 天 | 1–3650 天 | 先调用 Memory `scoped_forget`，成功后才删除本地身份 |
| 管理审计日志 | 180 天 | 7–3650 天 | 删除旧记录后写入本次清理摘要 |
| 共享 TTS 文件 | 24 小时 | 1–8760 小时 | 只删除服务生成目录中的过期 `.wav`/`.tmp` |
| 自动清理周期 | 60 分钟 | 5–1440 分钟 | 单实例互斥执行，也可由管理员手动触发 |

访客保留期始终被规范化为不短于消息保留期，因为公共消息仍可能包含作者 ID 和显示名。管理员提交更短的访客期限时，服务端会自动提升到消息期限。

## 一致性与失败语义

1. 清理最多选择 100 个过期访客，排除当前在线访客；
2. 对每个候选访客调用幂等的 Memory 遗忘接口；
3. Memory 遗忘成功的访客进入本地删除列表；
4. Memory 失败的访客保留在 SQLite，记录失败计数，并在下次清理重试；
5. 消息、事件、turn、幂等请求、审计和已确认访客在一个 `BEGIN IMMEDIATE` 事务中删除；
6. 共享语音随后按文件修改时间清理；
7. 最终结果写入 `retention_last_result`，供管理后台查看。

该顺序优先避免“本地身份已删除，但长期记忆仍存在”的隐私不一致。它不承诺 Memory 和 SQLite 之间的分布式原子事务，但失败方向始终是保留并重试，而不是静默留下不可寻址记忆。

## 重连协议

删除旧 `room_events` 后，服务端保留房间单调递增的 `last_seq`，并计算当前 `oldest_available_seq`。

客户端请求的 `after_seq` 已过期或领先于服务器时，WebSocket 在 `session.ready` 后发送：

```json
{
  "type": "replay.reset",
  "payload": {
    "reason": "history_expired",
    "requested_after_seq": 10,
    "replay_from_seq": 120,
    "last_room_seq": 120
  }
}
```

客户端必须清空本地时间线和去重集合，把游标重置到 `replay_from_seq`，再处理后续回放。不得把缺失的事件窗口当成“没有新消息”。

## 配置与操作

环境变量提供首次启动默认值；保存在 SQLite 的管理设置优先于环境默认值：

```dotenv
NEKO_PUBLIC_MESSAGE_RETENTION_DAYS=30
NEKO_PUBLIC_VISITOR_RETENTION_DAYS=90
NEKO_PUBLIC_AUDIT_RETENTION_DAYS=180
NEKO_PUBLIC_SPEECH_RETENTION_HOURS=24
NEKO_PUBLIC_CLEANUP_INTERVAL_MINUTES=60
```

管理后台可以修改策略、查看上次结果和立即执行清理。所有策略修改与执行都会进入审计日志。

## 非目标

- 自动清理不替代加密备份和异机恢复；
- 第一版不自动清理 Memory 的房间共享事实或受保护 Persona；
- 第一版不提供法律保全冻结；如有此需求，必须新增独立 ADR 和权限模型；
- 已进入备份的数据按备份保留策略处理，不能只依赖在线数据库清理。
