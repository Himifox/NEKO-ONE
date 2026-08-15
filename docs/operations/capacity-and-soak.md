# 容量与稳定性验证

> 状态：短时基线与正式 30 分钟档位已通过；24 小时运行尚未执行
>
> 更新日期：2026-08-15

## 验证对象

`scripts/verify_room_capacity.py` 使用真实的 `PublicRoomService`、`RoomConnectionHub`、`RoomDirector` 和 SQLite，只替换会产生费用或污染正式数据的 LLM、Memory、TTS 供应商。

每个 profile 都会建立指定数量的真实 Hub 连接，并自动验证：

- 所有连接收到从 1 开始、无重复、无缺口、严格递增的 `room_seq`；
- SQLite 消息、事件、turn 数量及访客回复归属完全一致；
- 任意时刻只有一个 NEKO generation；
- 每位访客的提交数与被回复数相同；
- 一个故意阻塞的慢连接被单独以 1013 断开，其他连接不受影响；
- 断开后所有 WebSocket writer 任务归零，服务关闭后相关任务归零；
- 输出提交 p95、Director 高水位、Python 内存、数据库/WAL 大小和任务高水位。

它不经过 Nginx、TCP、TLS，也不调用真实供应商。因此它证明房间核心并发不变量，不证明公网链路容量或真实模型延迟。

## 已执行的短时基线

环境：Windows 本地工作区、Python 3.11、确定性假供应商、每个 profile 每位访客同时提交 1 条、模型延迟 2ms。

执行命令：

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_room_capacity.py
```

2026-08-15 结果：

| 访客 | 提交 | 总耗时 | 提交 p95 | Director 峰值 | 最大 generation | 最终 seq | Python 峰值 | 连接任务残留 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 0.985s | 219ms | 9 | 1 | 30 | 0.154 MiB | 0 |
| 25 | 25 | 2.750s | 453ms | 24 | 1 | 75 | 0.233 MiB | 0 |
| 50 | 50 | 6.781s | 1390ms | 49 | 1 | 150 | 0.361 MiB | 0 |

补充高水位：50 人每人同时提交 3 条，共 150 条；最终 `room_seq=450`，最大 generation 为 1，Director 峰值 148，全部 writer 回收，总耗时 14.703 秒。

补充持续模式：50 人、目标 5 条/秒、10 秒准确提交 50 条；Director 峰值 1、提交 p95 47ms、最终 `room_seq=150`、全部 writer 回收。

这些结果只标记“短时基线通过”，不能勾选路线图中的 30 分钟或 24 小时门槛。

## 正式 30 分钟档位

以下命令会依次运行 10、25、50 人三个 profile，每个 profile 30 分钟，总时长约 90 分钟。全房间目标速率为 1 条/秒，访客按轮询均匀发言：

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_room_capacity.py `
  --profiles 10,25,50 `
  --duration-seconds 1800 `
  --messages-per-second 1 `
  --progress-seconds 30 `
  --output var/evidence/capacity-30m.json
```

通过条件：

- 进程退出码为 0，三个 profile 均有结果；
- `max_generation_concurrency == 1`；
- `writer_tasks_after_disconnect == 0`；
- `slow_client_isolated == true`；
- 50 人 profile 的持续流量提交 p95 小于 250ms，最终 drain 小于 30 秒；
- 无 SQLite locked、事件缺口、重复回复、未完成 turn 或相关任务残留；
- Python end 相对起点增长小于 32 MiB，并结合每 30 秒进度确认没有持续单调失控。

若机器性能导致延迟门槛不合适，应保留原始结果并通过 ADR 修改门槛，不得只在脚本中放宽断言后宣称通过。

### 正式结果（2026-08-15）

上述命令已完整执行约 90 分钟并以退出码 0 结束。提交到仓库的脱敏结果见 [`capacity-30m-2026-08-15.json`](../evidence/capacity-30m-2026-08-15.json)；包含全部 30 秒采样点的本地原始报告 SHA-256 为 `a6248d59188fc1cbcb6724f1cc177a1f2aae57db741f2a38906b562f0146993c`。

| 访客 | 提交 | 耗时 | p95 | Director 峰值 | 最大 generation | 最终 seq | Python 峰值/结束 | 最大任务数 | 慢连接隔离 | writer 残留 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: |
| 10 | 1800 | 1800.062s | 16ms | 1 | 1 | 5400 | 0.239/0.162 MiB | 13 | 是 | 0 |
| 25 | 1800 | 1801.016s | 16ms | 1 | 1 | 5400 | 0.245/0.156 MiB | 28 | 是 | 0 |
| 50 | 1800 | 1800.032s | 16ms | 1 | 1 | 5400 | 0.336/0.213 MiB | 53 | 是 | 0 |

三个档位均为 `3600` 条 SQLite 消息、`5400` 个事件和 `1800` 个完成 turn；drain 与 WAL 最终均为 0。任务高水位随连接数线性变化，断开后 writer 全部回收。该证据满足第一阶段的 10/25/50 人 30 分钟核心容量门槛，但不替代 24 小时 soak 和目标 Debian/VPS 公网链路验收。

## 24 小时核心 soak

该命令保持 50 个连接，每 10 秒发送一条房间消息，预计 8640 条：

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_room_capacity.py `
  --profiles 50 `
  --duration-seconds 86400 `
  --messages-per-second 0.1 `
  --progress-seconds 30 `
  --output var/evidence/capacity-24h.json
```

除全部脚本断言外，24 小时门槛要求：

- 没有任务数、内存或队列深度持续失控；
- Python end 相对起点增长小于 64 MiB；
- 最终 drain 小于 60 秒；
- 数据库可重新打开，事件数、消息数和 turn 数完全匹配；
- 中断运行不算通过，必须保留完整 24 小时结果。

`--output` 报告会保留每个进度周期的 `progress_samples`，包含 Python 内存、任务数、队列深度和已提交数量；验收时应查看完整曲线，不能只引用最终值。

## 预生产补充验收

核心 soak 通过后，还必须在目标 Debian/VPS 拓扑补充：

1. `nginx -t` 与外部端口扫描；
2. 50 个真实 WSS 连接的 Origin、Cookie、重连和 429/1013 行为；
3. 真实 LLM、Memory、至少一个 TTS 的低频端到端冒烟；
4. 记录 CPU、RSS、磁盘、首 token、模型总耗时和供应商错误率；
5. 服务重启后检查未完成 turn、断线续传和数据库完整性。

只有核心 24 小时 soak 与预生产检查都完成，路线图的“24 小时单实例稳定性”才能勾选。
