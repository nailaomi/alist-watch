# alist-watch

**Netwatch — 网盘凭证巡检器，给 alist / G-Box 用的「每天早上看一眼」面板。**

每天定时扫一遍 alist 的存储列表，告诉你哪个网盘的凭证要过期了、哪批分享链接已经失效，并把结论推到手机（Bark）。
纯 Python 标准库实现，单个容器，不写 alist 的任何数据。

---

## 为什么需要它

alist 挂载网盘时，token 过期和分享被上游取消都是**静默发生**的：WebDAV 照样挂载着，只有在真正点开某个目录时才会发现「Access token is expired」或者分享已经 404。

等到你需要用的时候才发现，通常已经失效好几天了。

这个工具做的就是：每天早中晚各检查一次，把「哪个盘坏了、坏在哪个存储、错误原文是什么」直接推到手机，附上修复链接。

## 特性

**巡检**

- 直接只读打开 alist 的 `data.db`，不需要 `docker.sock`，不需要 API Token
- 挂载路径自动发现（读 `x_storages` 的 `driver` + `mount_path`），不硬编码
- 区分**个人盘**（有账号）和**分享盘**（只有分享链接）：
  - 个人盘判断 `status`，凭证失效直接报错
  - 分享盘统计「可用 / 总数」，个人盘正常但分享全灭时降级提醒
  - **只监测填过账号的网盘**，纯分享盘挂载（如 `ThunderShare`）自动排除
- 实时鉴权探测：对网盘真的发一次 `POST /api/fs/list`，目标是随机子路径（绕开 alist 的目录缓存）
- 防封控：同一网盘默认 8 小时才实测一次，中间轮次复用上次结论
- **影音库更新**（可选，需 Emby）：看 Emby 最近一条入库离现在多久，超过阈值（默认 36 小时）就告警

**通知**

- Bark 推送，纯文字（不带图标和图片，手机上更好看）
- 分级：正常 `active`、异常 `timeSensitive`
- 点通知直达 alist 的存储管理页
- 降噪：同一批网盘连续失败且指纹相同，自动降到 `passive` 不打扰
- 每天默认 3 次（08:30 / 12:30 / 19:30），改时间不用重启容器

**界面**

- 单页 Web UI，可装成 PWA
- 明暗主题跟随系统，`prefers-reduced-motion` / `prefers-reduced-transparency` / `prefers-contrast` 三套降级
- 30 天可用率、本周异常轮次、连续全绿天数
- 每个网盘一条 7 天热力条；故障连续轮次自动合并成「事件」，显示起止时间与持续时长
- 抖动预警：近 7 天反复失效又恢复的网盘打「近期不稳」标签，提示重新登录
- 单盘重测（不写历史、不推送）、暂停 2/8 小时（换凭证时用，心跳照常）
- 配置可导出/导入 JSON

## 快速开始

前提：已经有一个跑着的 alist（或 G-Box），并且能拿到它的 `data.db`。

```bash
git clone https://github.com/nailaomi/alist-watch.git
cd alist-watch
```

编辑 `docker-compose.yml`，把最后一行换成你自己 alist 的 data 目录：

```yaml
    volumes:
      - ./data:/app/data
      - /path/to/alist/data:/alist/data:ro   # ← 改这里
```

如果 alist 也是 docker 部署，可以这样查它的数据目录：

```bash
docker inspect alist --format '{{json .Mounts}}'
```

启动：

```bash
docker compose up -d --build
```

打开 `http://<NAS-IP>:8099`，**默认密码 `changeme`**，登录后到「安全」页立刻改掉。

然后在「基础配置」里填：

| 字段 | 说明 |
|---|---|
| alist 地址 | 容器内访问宿主机用 `http://host.docker.internal:5678`（换成你的端口） |
| alist 数据库 | 容器内路径，与上面挂载的 `/alist/data` 对应，默认 `/alist/data/data.db` |
| Bark 地址 / Key | 手机装 Bark，把推送地址和 key 填进去 |
| G-Box 后台 | **可选**。填了才会检查 token 刷新链路，留空则跳过 |
| Emby 地址 / API 密钥 | **可选**。填了才会检查影音库更新，见下一节 |

## 影音库更新（可选）

如果你用 `xy-emd` 之类的工具每天把小雅的片单同步进 Emby，这里有个容易忽略的坑：**同步任务显示成功，不等于内容真的更新了**。小雅的源里缺某集时，同步程序只会报「下载失败 N 个文件」，任务本身仍然算完成。

所以这一项只问 Emby 自己的账：

- 最近一条入库的 `DateCreated` 离现在多久
- 今天入库了多少集
- 库里总共有多少集 / 多少部

超过阈值（默认 36 小时，给每日同步留足缓冲）没有新增，就判定为断更。这比读同步工具的日志更贴近你的实际感受——**文件落了盘但 Emby 没扫到，你在 Emby 里看到的还是旧的**。

为什么不去读同步工具的日志：那需要挂 `docker.sock` 或者存 NAS 密码走 SSH。`docker.sock` 等价于 root 权限，为一个日志把它交给一个带 Web 界面的程序，不值得。

在 Emby 后台 `设置 → 高级 → API 密钥 → 新建`，名字随便填，把生成的串贴进设置页即可。地址填容器内能访问到的 Emby，例如 `http://host.docker.internal:8096`（换成你自己的端口）。

## 它是怎么判断「坏了」的

三层，从便宜到贵：

1. **看 alist 自己的记录**：`x_storages.status` 不等于 `work` 就是坏的。这层零成本，每轮都跑。
2. **看分享盘存活率**：`status=work` 的分享盘数量 / 总数。个人盘正常但分享全灭，通常是上游取消了分享。
3. **真的打一次上游**：`POST /api/fs/list` 列一个随机不存在的子目录。
   - 返回「object not found」→ 鉴权通过，只是路径不存在
   - 返回 401 / token expired / cookie 失效 → 凭证真的坏了

第三层有被风控的风险，所以按网盘节流，默认 8 小时一次。一天 3 轮通知里，早上和晚上实测，中午复用结论。

## 数据与隐私

- 所有数据都在容器内的 `/app/data`：`config.json`（配置）、`history.db`（巡检历史）、`runstate.json`（运行状态）
- alist 的 `data.db` 是**只读挂载**，程序不会写入一个字节
- 不向任何第三方发送数据，除了你自己配置的 Bark 服务器
- `.gitignore` 已经排除 `data/`、`*.db`、`config.json`

## 安全提示

这是个内网工具，请按内网工具对待：

- **改掉默认密码**。`changeme` 只是占位；如果把它暴露到公网又不改密码，等于把网盘状态公开
- 需要外网访问时，建议放在反向代理后面并启用 HTTPS
- 登录态是 HMAC 签名的 Cookie，密码用 `hmac.compare_digest` 比较；也支持 HTTP Basic Auth（方便脚本调用）
- 默认不含任何外部依赖，`python:3.12-alpine` 基础镜像

## 可选：外部心跳

程序自己挂了是没法给自己报丧的。所以支持每轮结束向外部 ping 一次：

- [healthchecks.io](https://healthchecks.io)：填 `https://hc-ping.com/<uuid>`，失败时自动请求 `/fail`
- Uptime Kuma 的 Push Monitor 也可以

**心跳服务不要和这个容器装在同一台机器上**，否则断电时一起死，等于没配。

## 支持的网盘

按 alist 的 driver 名映射，内置：

| 分组 | 个人盘 driver | 分享盘 driver |
|---|---|---|
| 115 | `115 Cloud` | `115 CloudShare` / `115 Share` |
| 夸克 | `Quark` | `QuarkShare` |
| 阿里云盘 | `AliyundriveOpen` | `AliyundriveShare2Open` |
| 天翼 | `189CloudPC` / `189Cloud` | — |
| PikPak | `PikPak` | `PikPakShare` |
| 光鸭 | `GuangYaPan` | `GuangYaShare` |
| 迅雷 | `Thunder` | `ThunderShare` |
| 百度 | `BaiduNetdisk` | — |

其他 driver 也能跑，只是没有专属图标和中文名，会按 driver 原名显示。要加新的，改 `app.py` 里的 `DRIVER_MAP` 和 `PROVIDER_META` 即可。

## 关于图标

`logos/` 下是各家网盘的官方标识，仅用于在界面上**指代对应的服务**，版权与商标归各自公司所有，本项目不主张任何权利。如果你的使用场景不适合包含这些素材，直接删掉 `logos/` 目录即可，界面会回退到文字头像。

## License

[MIT](LICENSE)
