# 备份与恢复

> 状态：一致性工具和本机隔离恢复已完成；独立 Debian 主机恢复尚未执行
>
> 更新日期：2026-08-16

`.github/workflows/verify-public-runtime.yml` 会在独立的 GitHub Actions `debian:12-slim` 容器中，用合成的公共数据库、Memory 数据和私有配置执行备份、校验、隔离恢复、篡改检测和路径穿越拒绝。该检查证明工具可在目标 Debian 系列环境运行，但不替代使用生产备份完成的异机恢复演练。

2026-08-16 的 PostgreSQL 自动演练已在 Debian 12 + PostgreSQL 15 上通过，记录见 [GitHub Actions #31895702560](https://github.com/Himifox/NEKO-ONE/actions/runs/31895702560)。该运行使用真实 `pg_dump`/`pg_restore`，并覆盖 Memory SQLite、私有文件、篡改与路径穿越拒绝；它仍不替代使用生产备份完成的异机恢复演练。

## 恢复目标

第一版必须同时保护三类来源：

| 清单根 | 内容 | 一致性方式 |
| --- | --- | --- |
| `postgresql` | 房间、消息、事件、游客、turn、审核和运行设置 | `pg_dump --format=custom`；schema、逐表数量和序号高水位清单 |
| `public` | 会话/管理密钥、共享语音、Live2D 文件 | 普通文件稳定复制 |
| `memory` | Persona、Recent、Facts、Reflections、Memory SQLite/日志及服务端模型配置 | 停止 Memory 写入后快照；其中 SQLite 仍使用 online backup |
| `private-config` | `/etc/neko-public.env` 或等价私有环境文件 | 稳定复制；始终视为密钥材料 |

`scripts/manage_backup.py` 不复制 PostgreSQL 数据目录，也不直接复制活动 SQLite 文件。公共业务库生成一个自定义格式 `public-room.dump`，并记录 schema 版本、逐表数量、房间 `last_seq` 和事件高水位；Memory SQLite 快照会转换为自包含的 DELETE journal 模式，并记录完整性、外键和表清单。任何 `-wal`、`-shm` 或 `-journal` 都不会被当作备份内容。

## 安全边界

该工具生成的是**明文暂存快照**，因为仓库不能替运维方持有加密密钥。快照包含会话密钥、管理密钥、Persona、访客记忆和供应商凭据：

- 只允许写入 root/neko 可读的本机加密磁盘暂存目录；
- 离开主机前必须用组织管理的 `age`、GPG、KMS 或备份平台加密；
- 加密密钥不得和备份保存在同一主机或同一账号；
- 上传后校验加密对象，随后删除明文暂存；
- 备份保留期应独立于在线数据保留期，并能执行访客删除请求。

清单 SHA-256 能发现传输损坏和意外修改，不能替代签名或经过认证的加密。

## 创建每日快照

先确认 `/var/lib/neko` 是这台机器实际使用的 Memory/私有模型配置根。若部署路径不同，必须使用真实路径，不能照抄示例。

Memory 涉及多个文件之间的逻辑一致性，因此创建快照时暂停 Memory 服务。公共房间可继续运行并自动降级记忆；PostgreSQL 由 `pg_dump` 的一致事务快照保护。工具要求 dump 前后的 schema、数量和高水位一致，因此应在低流量窗口执行，变化过快时会安全失败并要求重试。

```bash
sudo systemctl stop neko-memory

sudo -u neko /opt/neko-one/.venv/bin/python \
  /opt/neko-one/scripts/manage_backup.py create \
  --output /var/lib/neko-backup-staging/2026-08-15T120000Z \
  --public-data /var/lib/neko-public \
  --environment-file /etc/neko-public.env \
  --memory-data /var/lib/neko \
  --private-config /etc/neko-public.env \
  --persona-version persona-2026-08-15

sudo systemctl start neko-memory
sudo systemctl is-active --quiet neko-memory
```

如果 `create` 失败，先恢复 Memory 服务，再调查 PostgreSQL 连接、`pg_dump` 版本、源数据持续变化、文件权限、磁盘空间或 Memory SQLite 完整性错误。不得加 `|| true` 继续上传失败快照。

立即校验明文快照：

```bash
sudo -u neko /opt/neko-one/.venv/bin/python \
  /opt/neko-one/scripts/manage_backup.py verify \
  --backup /var/lib/neko-backup-staging/2026-08-15T120000Z
```

随后使用已批准的加密工具制作离机对象。以下只展示流程，不规定密钥体系：

```bash
tar -C /var/lib/neko-backup-staging -czf - 2026-08-15T120000Z \
  | age -r AGE_RECIPIENT_FROM_SECRET_MANAGER \
  > /var/lib/neko-backup-encrypted/2026-08-15T120000Z.tar.gz.age
```

上传、远端校验和明文删除应由受监控的备份任务完成。不要把真实 recipient、密钥或对象存储令牌写进仓库。

## 隔离恢复

恢复命令永远拒绝覆盖现有目录，只能发布到一个全新的隔离目录：

```bash
/opt/neko-one/.venv/bin/python scripts/manage_backup.py restore \
  --backup /srv/restore/source/2026-08-15T120000Z \
  --destination /srv/restore/result/2026-08-15T120000Z
```

恢复后目录为：

```text
result/2026-08-15T120000Z/
  data/postgresql/public-room.dump
  data/public/
  data/memory/
  data/private-config/
  restore-report.json
```

文件恢复会在原子发布前重新检查所有 SHA-256，使用 `pg_restore --list` 检查 PostgreSQL 归档，并打开每个恢复后的 Memory SQLite 执行完整性、外键和元数据比对。任一文件缺失、增加、损坏、路径逃逸或语义不一致都会使恢复失败。

PostgreSQL 必须恢复到新建的空数据库。准备一个只在隔离主机使用、权限为
`0600` 的环境文件，例如：

```dotenv
NEKO_POSTGRES_RESTORE_URL=postgresql://neko_restore:...@127.0.0.1:5432/neko_restore_20260816
```

数据库名必须由操作员再次明确确认；目标存在任何 public 表都会被拒绝：

```bash
/opt/neko-one/.venv/bin/python scripts/manage_backup.py restore-postgres \
  --backup /srv/restore/source/2026-08-15T120000Z \
  --environment-file /etc/neko-restore.env \
  --confirm-empty-database neko_restore_20260816
```

`pg_restore` 使用 `--single-transaction --exit-on-error`。恢复后工具重新比对
schema 版本、逐表数量、房间 `last_seq` 和事件高水位；失败时不得在该数据库上
手工补洞，应保留日志后丢弃整个隔离数据库并重新演练。

## 独立机器恢复演练

路线图门槛只能由一台不共享原数据卷的 Debian 主机证明。每次演练记录：

1. 备份对象 ID、创建时间、应用 commit、Persona 版本和加密方式；
2. 恢复主机、操作人、开始/完成时间、RTO 和总字节数；
3. `manage_backup.py verify`、`restore` 与 `restore-postgres` 的完整输出；
4. 用恢复的环境文件启动 loopback-only Memory 与公共服务；
5. 管理员登录、房间 `last_seq`、历史消息、Persona、访客隔离记忆和共享语音抽样；
6. 创建一个恢复后的新 turn，确认序号从清单高水位继续且没有覆盖旧消息；
7. 停止隔离服务并安全销毁恢复出的明文密钥和访客数据。

以下任何情况都不算通过：只解压不启动、在原主机原数据卷恢复、跳过 Memory/私有配置、使用修改后的生产数据补洞、没有记录恢复时间，或把本机确定性验证当成异机证据。

## 仓库内回归

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_backup_restore.py
```

该脚本只允许在名称含 `test`/`verify`/`ci` 的受控数据库中执行。它验证真实 `pg_dump`/`pg_restore`、非空目标拒绝、Memory SQLite/Persona、私有配置、备份后写入隔离、损坏检测和恶意清单路径拒绝，不会读取或修改正式数据。
