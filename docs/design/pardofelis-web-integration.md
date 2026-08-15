# ADR：与 pardofelis-web 的第一版集成

> 状态：第一版方案已确定，等待 DNS、OpenResty 和网站入口上线
>
> 日期：2026-08-15
>
> 目标站点：`https://pardofelis.wiki/`
>
> 宿主仓库：`Himifox/pardofelis-web`

## 1. 已确认的现状

`pardofelis-web` 是 Vue 3、TypeScript、Vite 构建的静态个人站，生产内容由 GitHub Actions 通过 `rsync --delete-delay` 发布到 1Panel 管理的 OpenResty 网站目录。目标主机是 Debian，`pardofelis.wiki` 目前经过 Cloudflare 对外提供 HTTPS。

这带来三个直接约束：

1. NEKO-ONE 是有 WebSocket、SQLite、Memory、LLM 和 TTS 的有状态服务，不能合并进 Vite 静态产物。
2. `pardofelis-web` 每次部署会删除不属于其 `dist/` 的文件，不能把 NEKO 资源手工放进该站点目录。
3. 现有 OpenResty 已占用公网 80/443，NEKO-ONE 不能再启动第二个公网 Nginx；它只监听回环地址，由现有 OpenResty 反向代理。

## 2. 第一版决策

第一版使用以下边界：

```text
访问者
  ├─ https://pardofelis.wiki/       → Cloudflare → 1Panel OpenResty → 静态 pardofelis-web
  └─ https://neko.pardofelis.wiki/  → Cloudflare → 1Panel OpenResty → 127.0.0.1:48911
                                                                ├─ NEKO public room
                                                                └─ loopback Memory/TTS upstreams
```

- `pardofelis-web` 只增加一个普通 HTTPS 链接或入口卡片；
- NEKO 页面、API、WebSocket、Live2D 和共享语音全部由 NEKO-ONE 服务；
- 两个仓库独立构建、独立回滚，任何一方发布都不会覆盖另一方文件；
- 第一版不使用 iframe，也不从博客页面直接调用 NEKO API；
- 管理后台仍由 NEKO-ONE 保护，不进入 `pardofelis-web` 构建产物。

普通链接避免了 iframe 的第三方 Cookie、点击劫持、移动端尺寸、音频自动播放和双重滚动问题，也与当前 `frame-ancestors 'none'` 策略一致。

## 3. 域名与边缘配置

目标公共地址固定为 `https://neko.pardofelis.wiki/`。截至 2026-08-15，只确认 `pardofelis.wiki` 正常经过 Cloudflare，`neko.pardofelis.wiki` 尚未建立 A/AAAA/CNAME 解析，因此仍属于上线待办。

上线顺序：

1. 在 Cloudflare 建立 `neko.pardofelis.wiki` 记录，指向与主站相同的 Debian VPS；
2. 在 1Panel/OpenResty 创建该子域站点并签发有效证书；
3. 把 NEKO 的 `map`、限流区和子域 `server` 配置加入 OpenResty 的 `http` 上下文；
4. 反向代理只指向 `http://127.0.0.1:48911`，保留 WebSocket Upgrade、超时和限流；
5. 配置 Cloudflare 支持 WebSocket，并确认安全规则不会挑战 `/ws/`；
6. 先从外网验收 NEKO，再在 `pardofelis-web` 增加入口链接。

共享 OpenResty 已经拥有默认站点时，不应重复安装 Nginx 或添加第二组 `default_server`。`deploy/nginx-neko-public.conf` 是完整安全基线；合并进 1Panel 时沿用平台已有的未知 Host 拒绝规则，只加入 NEKO 使用的全局限流声明和子域 server。

## 4. NEKO 生产环境值

第一版至少使用：

```dotenv
NEKO_PUBLIC_HOST=127.0.0.1
NEKO_PUBLIC_PORT=48911
NEKO_PUBLIC_ALLOWED_ORIGINS=https://neko.pardofelis.wiki
NEKO_PUBLIC_ALLOW_MISSING_ORIGIN=0
NEKO_PUBLIC_SECURE_COOKIE=1
```

由于页面和 WebSocket 同源，不需要为 `https://pardofelis.wiki` 开放 WebSocket Origin。以后若博客直接调用 API 或使用 iframe，必须先新增 ADR，并重新处理 CORS/Origin、Cookie、CSP、音频和点击劫持边界。

## 5. 资产边界

`pardofelis-web` 当前包含插画、动画图集、PSD 和 Live2D 制作中间素材，但这些文件不等于可运行的 Cubism `model3.json` 模型，也不能单凭“存在于仓库”证明具有公开 AI 角色服务授权。

- 生产 Cubism 模型和动作只部署到 `/var/lib/neko-public/live2d/<name>/`；
- NEKO-ONE 公开仓库继续不捆绑角色模型和音色；
- `pardofelis-web` 的静态插画不被 NEKO-ONE 构建复制；
- 生产模型、动作、字体和音色必须分别保留来源、权利人、授权范围和摘要证据。

## 6. pardofelis-web 后续改动

NEKO 子域通过公网验收后，再在 `pardofelis-web` 完成一个小而独立的改动：

- 在幸运收藏室或导航中加入“和 NEKO 聊天”入口；
- 使用普通 `<a href="https://neko.pardofelis.wiki/">`，不携带访客标识或查询参数；
- 不在静态仓库加入 Key、Memory 地址、管理链接或 Persona 配置；
- 构建、类型检查和现有自动部署全部通过后再发布。

该改动应在 `pardofelis-web` 自己的提交历史中完成，不能混入 NEKO-ONE 仓库。

## 7. 联合验收

- `pardofelis.wiki` 和 `neko.pardofelis.wiki` 的证书、重定向和安全响应头正确；
- 主站入口不产生 404，也不会被 `rsync --delete-delay` 删除；
- NEKO 的游客 Cookie 只属于 NEKO 子域，不由静态站读取；
- 50 个 WSS 连接下 Origin、重连、续传和 1013 行为符合容量手册；
- Cloudflare、OpenResty 和应用三层都能区分 429、413、403 与 5xx；
- 只对公网暴露 443，NEKO API、Memory、TTS 和管理凭据不直接监听公网地址；
- 两个仓库均能独立回滚，回滚主站不会触碰 NEKO 数据卷。
