# 真实供应商验收

> 状态：预检、费用闸门和确定性回归已完成；真实 LLM、Memory、TTS 调用尚未执行
>
> 更新日期：2026-08-15

## 为什么单独验收

假供应商回归能证明房间协议和故障降级，但不能证明运营配置里的模型名、URL、密钥、Voice、Memory 端口以及供应商账户当前可用。真实验收会产生费用，也会短暂写入一条隔离访客记忆，因此不能在普通测试或 CI 中自动触发。

`scripts/provider_acceptance.py` 提供两种模式：

- `preflight` 只读取服务器配置、解析 TTS 路由并探测 loopback Memory `/health`，不调用付费接口；
- `live` 流式调用一次真实 LLM，向独立 `acceptance_*` 访客作用域写入并读取 Memory，合成一次真实 WAV，随后遗忘该访客并删除本地音频。

所有报告只记录布尔状态、供应商类型、耗时、chunk 数、字符数和 WAV 元数据，不记录 API Key、完整 URL、Prompt、回复正文或记忆正文。

## 只读预检

先启动 Memory 服务并在与生产相同的服务用户和配置目录下执行：

```bash
sudo -u neko /opt/neko-one/.venv/bin/python \
  /opt/neko-one/scripts/provider_acceptance.py preflight \
  --output /var/lib/neko-public/evidence/provider-preflight.json
```

退出码为 0 且 `ready_for_live_acceptance=true` 才能进入真实模式。预检失败时按 `conversation`、`memory`、`tts` 三段修复，不要把密钥复制到工单或公开日志。

## 真实调用

确认测试账户、额度、模型和 Voice 允许公开产品使用后，显式输入费用确认串：

```bash
sudo -u neko /opt/neko-one/.venv/bin/python \
  /opt/neko-one/scripts/provider_acceptance.py live \
  --acknowledge I_ACCEPT_PROVIDER_COSTS_AND_TEST_DATA \
  --output /var/lib/neko-public/evidence/provider-live.json
```

通过条件：

- LLM 至少产生一个流式 delta 和非空回复，并记录首 chunk/总耗时；
- Memory 健康指纹正确，隔离作用域写入及上下文读取成功；
- 无论 TTS 成功还是失败，`memory.cleanup_passed=true`；
- TTS 返回单声道、16-bit、非空 WAV，并在检查后删除本地验收音频；
- `passed=true`，命令退出码为 0，证据文件不含密钥和正文；
- 运维记录供应商账户、模型/Voice、区域、执行人和时间，但敏感值留在秘密管理系统。

真实模式失败也会输出结构化阶段和错误类型；不得因 LLM 成功就忽略 Memory 清理或 TTS 失败。若清理失败，立即通过管理工具对报告中的 `acceptance_subject` 执行遗忘，并记录事故。

## 仓库回归

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_provider_acceptance.py
```

该脚本只使用假 LLM、Memory 和 TTS，覆盖流式输出、作用域写入/读取、WAV 校验、音频删除、正常遗忘，以及故意 TTS 失败后仍执行遗忘和 shutdown。它不会产生供应商费用。
