# NEKO-ONE 运维手册

- [Debian 自动验证](../../.github/workflows/verify-public-runtime.yml)：在 Debian 12 容器中执行真实 `nginx -t`，并运行公共房间、Memory、本地备份恢复、安全边界、资产与构建检查；不会调用付费供应商。
- [容量与稳定性验证](./capacity-and-soak.md)：10/25/50 人短时基线、30 分钟容量档位和 24 小时 soak 的执行及验收规则。
- [备份与恢复](./backup-and-restore.md)：在线 SQLite 快照、清单校验、加密交接、隔离恢复和异机演练。
- [公网资产授权](./public-assets.md)：第三方运行库清单、运营方 Live2D/TTS 授权证据和自动防回归。
- [真实供应商验收](./provider-acceptance.md)：无费用预检、显式费用闸门、隔离 Memory 清理和真实 LLM/TTS 证据。
- [公网部署](../../deploy/README.md)：Nginx、systemd、网络边界与上线前检查。

运维文档中的“通过”必须有命令、时间、配置和机器信息支撑。短时冒烟不能替代长时验收，确定性假供应商验证也不能替代真实供应商冒烟。
