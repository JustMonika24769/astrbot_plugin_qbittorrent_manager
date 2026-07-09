# AstrBot qBittorrent Manager

在 QQ 群通过 AstrBot 搜索 PT 站种子，并把选中的种子上传到 qBittorrent 下载。默认搜索北洋园 PT（天津大学 PT 站），也可以通过配置切换到其他 NexusPHP 风格 PT 站。

## 功能

- `/种子 关键词`：搜索配置的 PT 站，并使用 AstrBot t2i 服务渲染结果卡片。
- `/种子下载 序号`：下载对应 `.torrent` 文件并上传到 qBittorrent。
- `/种子帮助`：显示使用说明。
- 自动缓存每个用户最近一次搜索结果，避免群内多人选择互相干扰。
- 对 Cookie 缺失、登录失败、序号错误、搜索无结果、渲染失败等情况做友好提示。

## 配置

安装插件后在 AstrBot 插件配置页面填写：

- `provider_cookie`：PT 站登录 Cookie。未配置时，大多数 PT 站无法搜索或下载。
- `qb_url`：qBittorrent WebUI 地址，例如 `http://127.0.0.1:8080`。
- `qb_username` / `qb_password`：qBittorrent WebUI 登录账号。
- `qb_save_path` / `qb_category`：可选保存路径与分类。
- `cache_ttl_seconds`：搜索结果等待选择的超时时间，默认 `600` 秒，也就是 10 分钟。
- `render_width`：结果卡片渲染宽度，默认 `900`，高度会按结果数自动计算。

如果使用其他 PT 站，修改：

- `provider_base_url`：站点根地址。
- `provider_search_path`：搜索路径，必须包含 `{keyword}`，例如 `torrents.php?search={keyword}&incldead=0`。

## 使用

```text
/种子 Ubuntu
/种子下载 1
```

## 注意

- 请确保 qBittorrent WebUI 已启用，并允许 AstrBot 所在机器访问。
- PT Cookie 属于敏感信息，请不要在群聊中发送。
- 本插件只负责搜索和添加任务，请遵守站点规则和所在地法律法规。
