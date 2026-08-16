# 公网资产授权

> 状态：仓库内公开资产审计已完成；生产角色模型和公开音色仍须由运营方提供授权证据
>
> 更新日期：2026-08-15

## 审计结论

仓库曾包含一个没有来源、作者或许可文件的 `yui-lolita` 模型压缩包，以及一个与页面接口不匹配、没有授权标头的旧 Live2D 运行库。二者已经永久删除，不能因为“原项目能运行”就推定拥有公网再分发权。

当前公开发行物只包含三项浏览器运行库：

| 资产 | 版本 | 许可 | 处理 |
| --- | --- | --- | --- |
| PixiJS | 7.4.3 | MIT | npm 官方包内容与仓库 SHA-256 一致，附完整 MIT 文本 |
| pixi-live2d-display Cubism 4 bundle | 0.5.0-beta | MIT | 来自 npm 官方包；把 `process.env.NODE_ENV` 固化为 production，并补充指向随包 MIT 文本的分发标头 |
| Live2D Cubism Core for Web | 文件内未提供可证明的精确版本 | Live2D Proprietary Software License Agreement | 文件标头明确标为 Redistributable Code；保留标头、精确 SHA-256 与协议链接 |

权威机器清单位于 `static/libs/manifest.json`。项目自身 Apache-2.0 许可证不会覆盖或改写上述第三方条款。

## 不随仓库分发的内容

- 任何 `.moc3`、`.model3.json`、纹理、动作、表情或角色音频；
- 字体文件；页面只使用操作系统字体回退；
- 默认 TTS 声音或克隆声音；TTS 由服务端运营配置决定；
- 模型作者素材、商店下载物或来历不明的桌面版资源。

因此克隆公开仓库后，页面会显示“尚未配置已授权的 Live2D 模型”，而不是暗中解包未知角色。

## 安装运营方模型

运营方应把自有或已获明确许可的 Cubism 4 模型放到：

```text
$NEKO_PUBLIC_DATA_DIR/live2d/<model-name>/
  <model-file>.model3.json
  <model-file>.moc3
  textures...
  optional motions / expressions / physics...
```

启动服务后，在私有管理后台的“形象配置”中选择并启用模型。后台只列出完成
descriptor 资产校验的候选项，选择结果保存在 PostgreSQL，重启后继续生效。

也可以用以下环境变量提供首次启动的默认选择：

```dotenv
NEKO_PUBLIC_LIVE2D_MODEL_NAME=<model-name>
NEKO_PUBLIC_LIVE2D_MODEL_FILE=<model-file>.model3.json
```

数据库中已有后台选择时会覆盖环境变量。名称和 descriptor 只能是安全文件名。服务端会解析 descriptor，拒绝路径逃逸，并确认 Moc、纹理及声明的 Physics、Pose、UserData、DisplayInfo、Expression、Motion、Sound 文件都存在；失败时保持文字聊天可用并显示明确占位状态。

`/live2d-assets` 只会公开当前启用 descriptor 和它明确引用的文件。同一数据目录中的其他模型、说明、订单或临时文件即使能被猜到路径也会返回 404；但仍不应把授权证明或密钥放进模型目录。通过后台切换时，公开文件 allowlist 会立即原子更新；已经打开的公共房间刷新页面后加载新形象。直接替换当前模型的同名文件时仍建议重启服务并刷新浏览器缓存。

## 上线证据

路线图的资产许可门槛只有在生产所选模型和声音完成以下记录后才能勾选：

1. 作品名称、作者/权利人、原始购买或下载地址；
2. 许可正文或订单，明确允许网页展示、商业/非商业场景、并发访客和必要的服务器传输；
3. 是否允许修改动作、表情、纹理和与 AI/TTS 结合；
4. 必需署名、禁止场景、地域、期限与撤回条件；
5. Live2D SDK 发布许可是否适用于运营主体、收入规模和该 Web 应用；
6. TTS 供应商和具体 voice 对公开播放、缓存及多人广播的许可。

若任一项无法证明，生产环境必须保持该模型或声音关闭。此文档是工程审计，不是法律意见；涉及商业上线时应由权利人或合格法律顾问确认。

## 可重复验证

```powershell
uv --cache-dir .uv-cache run --locked python scripts/verify_public_assets.py
```

验证会核对第三方清单、文件 SHA-256、MIT 许可证、Cubism Core Redistributable Code 标头、前端加载顺序与 Live2D descriptor allowlist，并阻止模型、声音或字体文件重新混入公开发行物。
