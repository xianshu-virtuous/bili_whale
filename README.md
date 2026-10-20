# bili_whale —— B站账号管家（深深专用）

让她像主人一样用 B 站：查账号、看视频弹幕、翻会员购订单、替主人发评论发弹幕。

作者：**Aemeath**　｜　适用：Neo-MoFox ≥ 1.2.0

---

## 它解决什么问题

主人的 B 站账号里有真信息：未读消息、会员购订单（哪单要到仓库补尾款、快递单号、
收货地址）、收藏夹、观看历史。这些原本只有主人自己能在 App 里翻。

这个插件把这些能力交给 bot，**方式很轻**：登录态只读一份 `cookies.json`
（从 Playwright 一次性导出的完整 cookie，含 httpOnly 的 SESSDATA），之后全部走
纯 HTTP + wbi 签名 —— 不常驻浏览器、不占 MCP 调用、一条查询两百 token 上下。

顺带给了她一个很好用的副产物：**用弹幕看懂视频**。B 站字幕不是每个视频都有
（实测很多视频 `subtitles: []`），但弹幕永远有——按时间轴铺开之后，观众在吐槽什么、
UP 主算错了哪里，全都在里面。比抽帧截图省一个数量级。

## 依赖 MCP？是的，但不阻塞

登录态过期那天，最省事的重新导出方式就是 **playwright MCP**：打开 B 站 →
`Network.getAllCookies` → 写成 json。所以插件加载时会查一眼 MCP 管理器：

- 检测到 `playwright` → 日志一句 info，走人；
- 没检测到 → 日志一条 **warning**（列出当前连了哪些），然后**照常工作**。
  已有登录态能用到过期为止；真过期了，工具会明确回「需要重新导出一份 cookie」，
  而不是静默失败。

这个检查不参与任何功能判断，纯粹是提前打个招呼。

## 怎么配置（从零到能用）

一共四步，第 1 步最麻烦但只做一次。

### 第 1 步 · 拿到登录态

插件不存账号密码，只用一份 cookies。这份 cookies 从浏览器里导出来，**含 httpOnly
的 SESSDATA**——所以不能靠 `document.cookie` 拿，得走 CDP。

**1.1 确认 playwright MCP 可用**

`config/mcp.toml` 里要有这一段：

```toml
[mcp]
enabled = true

[mcp.stdio_servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest", "--browser", "chrome"]
```

重启框架，工具列表里能看到 playwright 那批工具就说明连上了。

**1.2 在浏览器里登录 B 站**

让 playwright 打开 `https://www.bilibili.com/`，扫码登录一次。
MCP 的浏览器是持久化 profile，登录一次之后 cookie 会留在里面。

**1.3 导出全部 cookie**

用 playwright 的 CDP 拿 httpOnly cookie（这一步必须在页面上下文里做）：

```js
const client = await page.context().newCDPSession(page);
const { cookies } = await client.send('Network.getAllCookies');
const bili = cookies.filter(c => c.domain.includes('bilibili'));
return JSON.stringify(Object.fromEntries(bili.map(c => [c.name, c.value])));
```

**1.4 存成文件**

把返回的那串 JSON 存到：

```
<框架根>/data/bili_cookies.json
```

格式是扁平字典，长这样：

```json
{
  "SESSDATA": "<必填：登录态核心，httpOnly，只能靠 CDP 拿>",
  "bili_jct": "<必填：csrf token，写操作用，缺了只能读>",
  "DedeUserID": "<必填：你的 uid>",
  "DedeUserID__ckMd5": "<选填>",
  "buvid3": "<必填：设备指纹，缺了容易被风控>",
  "b_nut": "<选填>",
  "_uuid": "<选填>",
  "buvid_fp": "<选填>",
  "bili_ticket": "<选填：会过期，过期了插件自己会重新要>",
  "bili_ticket_expires": "<选填>"
}
```

> 上面全是占位符。**别把真实取值贴进任何仓库、文档、日志或对话里**——
> 这份文件等价于账号权限。

**至少要有这四项**：`SESSDATA`、`bili_jct`、`DedeUserID`、`buvid3`。
`bili_jct` 就是写操作用的 csrf token，缺了它只能读不能写。

> 有效期一般几个月（`SESSDATA` 里的时间戳能看出到期日）。过期后工具会明确回
> 「需要重新导出一份 cookie」，照上面重做一遍即可。

### 第 2 步 · 写配置文件

路径：`config/plugins/bili_whale/config.toml`（框架首次加载插件时会给出默认配置）

```toml
[plugin]
enabled = true
cookie_file = "data/bili_cookies.json"
check_mcp = true
mcp_server_name = "playwright"

[actions]
enabled = true           # 写操作总闸（这个插件给主人自己用，默认开）
allow_reply = true
allow_danmaku = true
allow_like = true
allow_fav = true

[query]
history_limit = 12
danmaku_limit = 40
order_todo_only = false

[watch]
enabled = true
frames = 3
browser_profile = "data/bili_browser_profile"
screenshot_dir = "data/bili_watch"
page_timeout = 25.0
```

逐项说明：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 关掉＝一个工具都不注册 |
| `plugin.cookie_file` | `data/bili_cookies.json` | 相对**框架根**；也可以写绝对路径 |
| `plugin.check_mcp` | `true` | 加载时查一眼 playwright MCP，没有就打 warning（不影响功能） |
| `plugin.mcp_server_name` | `playwright` | 要查的 MCP 服务名，和你 `mcp.toml` 里的键一致 |
| `actions.enabled` | **`true`** | 写操作总闸。默认开是因为这插件挂在主人自己号上、也只给主人用；装到公共实例上建议关 |
| `actions.allow_reply` | `true` | 发视频评论 |
| `actions.allow_danmaku` | `true` | 发弹幕（删不掉，不想让它发就关这项） |
| `actions.allow_like` | `true` | 点赞 / 取消点赞（可逆） |
| `actions.allow_fav` | `true` | 收藏 / 取消收藏（可逆） |
| `query.history_limit` | `12` | `history` 默认返回条数（上限 30） |
| `query.danmaku_limit` | `40` | 抓弹幕默认条数（上限 200），越大越费 token |
| `query.order_todo_only` | `false` | `orders` 默认是否只看未完成的 |
| `watch.enabled` | `true` | 允许起浏览器看画面；关掉后 `bili_watch` 只回信息+弹幕 |
| `watch.frames` | `0` | 强制帧数，0=按时长自动（3分钟内2帧、15分钟内3帧、30分钟内4帧、更长5帧） |
| `watch.browser_profile` | `data/bili_browser_profile` | 浏览器用户目录，**必须是独立空目录**，别指到日常 Chrome 或 MCP 的 profile |
| `watch.screenshot_dir` | `data/bili_watch` | 截图存放目录 |
| `watch.page_timeout` | `25.0` | 等视频元素出现的超时秒数，网络慢可调大 |
| `watch.describe` | `true` | 抽完帧交给视觉模型描述（关掉就只回截图路径） |
| `watch.vision_task` | `video` | 用哪个模型任务，对应 `model.toml` 的 `[model_tasks.video]` |

**想装到公共/别人的实例上**：把 `[actions] enabled` 改成 `false`，就只剩只读 + 看视频。

**视觉描述依赖模型配置**：`config/model.toml` 里要有 `[model_tasks.video]` 且绑定的是
多模态模型（这台机器上是 `Qwen/Qwen3-VL-32B-Instruct`）。没有的话会自动降级成
「只给截图路径」，不影响其他功能。

### 第 3 步 · 验证

用实例自带 venv（别用系统 python）：

```powershell
$env:NEO_ROOT="F:\Neo-MoFox-Aemeath"
& "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" plugins\bili_whale\tests\check_logic.py
```

看到「查账号 — 你的昵称（LvX）」就说明登录态通了。想只验证加载不碰网络：

```powershell
& "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" plugins\bili_whale\tests\check_import.py
```

### 第 4 步 · 写操作（默认已经开着）

不用额外做什么，装完就能评论、发弹幕、点赞、收藏。想收窄就改：

```toml
[actions]
enabled = true          # 总闸
allow_reply = false     # 不想让它发评论就关
allow_danmaku = false   # 弹幕删不掉，建议按需
```

全关掉（`enabled = false`）就退化成「只读 + 看视频」。

**注意**：评论和弹幕是**发出去删不干净**的。它调 `bili_action` 前应该先问你；
但工具层不强制（工具描述里写了「动之前先跟主人确认」），
真正兜底的是你打开/关闭哪几项。

### 常见问题

**Q：日志说「没检测到 MCP 服务『playwright』」**
没事，不影响读写。只在 cookie 过期那天需要它——那时去 `mcp.toml` 把 playwright 段
打开，按第 1 步重新导一份。

**Q：工具回「登录态里没有 SESSDATA」/「需要重新导出一份 cookie」**
`data/bili_cookies.json` 丢了、格式不对、或者过期了。重做第 1 步。

**Q：工具回「写操作现在是关着的」**
`[actions] enabled` 还是 `false`。这是设计如此，改配置而不是让模型找绕路。

**Q：弹幕总是 0 条**
旧版本有这个问题（B 站返回 raw deflate，被当成文本了），1.0.0 已修。如果你是自己
改过 `client.py` 的 `_open`，确认 deflate 分支两种 wbits 都试了。

## 四个工具

| 工具 | 作用 |
|---|---|
| `bili_catalog` | 能力目录：每件事该调哪个工具、哪个 action、target 填什么。不确定就先查它 |
| `bili_query` | 只读：`account` / `unread` / `orders` / `order` / `video` / `danmaku` / `history` / `favs` / `followings` |
| `bili_watch` | **看视频**：链接进去 → 信息 + 弹幕 + 抽帧画面 |
| `bili_action` | 写：`reply` / `danmaku` / `like` / `unlike` / `fav_add` / `fav_del` |

为什么只给四个：B 站能做的事有二十来件，一件一个工具会把工具面撑爆（`tool_hub`
实测过 88 个工具占掉 53% 输入）。这里按「目录 / 只读 / 看视频 / 写」折成四个入口，
具体动作走 `action` 参数——schema 小、命中缓存、模型也好记。

## 看视频（`bili_watch`）

这是本插件最花心思的一块。给三样东西：

1. **信息** —— 标题 / UP / 时长 / 播放 / 简介（纯 HTTP，几十毫秒）；
2. **弹幕** —— 按时间铺开。别小看这个：B 站字幕不是每个视频都有（实测大量视频
   `subtitles: []`），但弹幕永远有，观众在吐槽什么、UP 主算错了哪里全在里面；
3. **画面** —— 按时间点抽帧，然后**交给框架的视觉模型描述成文字**。

### 画面描述（这一步才是「看懂」）

抽出来的帧对模型没用（它看不到磁盘），所以抽完立刻走一趟视觉模型：

```
model.toml  [model_tasks.video]  →  Qwen/Qwen3-VL-32B-Instruct
```

插件内部发一次独立请求（`request_name="bili_watch"`，**不进主对话上下文**），
把压到 720px 的帧按时间顺序塞进去，让它「只讲画面里确实能看到的东西」。
实测输出长这样：

```
第1张：男性主讲人面对镜头讲解，背景为深色窗帘、电脑显示器（蓝色科技图案），
       左上角有"老烟斗官方"水印。
第2张：画面切换为信息图表，左侧小窗仍显示主讲人，背景是 7-Eleven 标志。
第3张：主讲人再次出现，字幕显示"印度快商务单月GMV约1100亿卢比（约76亿人民币）"。
```

**为什么不用把图发给主人自己看**：一张 2 倍放大的帧有 3~4MB，进对话上下文太贵；
插件内部压到 720px 再送模型，回来的是一段 200 字——这才是「bot 看懂了画面」。

想关掉这段（省模型调用）：`[watch] describe = false`，就只回截图路径。

### 抽几帧、抽在哪

固定抽 3 帧是错的。视频信息密度不均匀——有的段落全是干货（弹幕密集），
有的纯过渡（几十秒没人说话）。所以：

**帧数按时长自适应**：

| 时长 | 帧数 |
|---|---|
| ≤ 3 分钟 | 2 |
| ≤ 15 分钟 | 3 |
| ≤ 30 分钟 | 4 |
| 更长 | 5 |

**位置一半给弹幕热度、一半均匀**：

1. 全量弹幕按时间分桶（约 40 段），掐掉首尾各 10%（片头/演职员表）；
2. 取弹幕最密集的几段中心点——观众在追着讨论的地方 ≈ 内容重点；
3. 另一半均匀铺开，保证覆盖全片，不至于全是吐槽点；
4. 挨太近的合并，免得两次截到同一画面。

弹幕热度只是「便宜的代理指标」，所以只占一半——全跟它走会漏掉没人说话但重要的段落。

### 截图只截视频区域

早期版本截整屏，结果导航栏、右侧推荐、评论全在里面——模型不需要看这些。
现在：

1. 先把播放器撑到整窗（顺便藏掉顶部导航条）；
2. 用 CDP 的 `clip` 精确裁到 `<video>` 的矩形；
3. 再放大 2 倍（小窗口里视频区只有 800×450，放大后细节才够认字）。

代价是单帧文件从 550KB 涨到 2~4MB——但反正要压到 720px 才送模型，不影响。

### 为什么不用无头浏览器

B 站对无头有检测，无头跑容易触发风控。所以这里起的是**真实的有头 Chrome**，
只是把窗口丢到屏幕外并最小化。

### 怎么做到不抢焦点

三道保险叠着来（每一道都是实测踩出来的）：

1. 进程级：`STARTUPINFO` 的 `SW_MINIMIZE`，一起来就是最小化的；
2. 窗口级：`--window-position=-3200,-3200` + `--start-minimized`；
3. 连上 CDP 后立刻 `Browser.setWindowBounds` 再压一次最小化。

只靠 `--start-minimized` 压不住——实测窗口会抢一次前台，把主人正在看的界面顶掉。

### 登录态从哪来

插件的浏览器 profile 是**独立空目录**（不能和 playwright MCP 共用同一目录）。
所以看完页面是未登录的——B 站未登录只给试看 30 秒、没高清。解决办法是启动后
用 CDP `Network.setCookie` 把 `cookies.json` 里的东西**注入进去**（成功注入十几条），
页面直接就是登录态。

### 降级

浏览器起不来 / 没装 `websocket-client` / 被风控挡住，**都不算失败**：照样返回
信息 + 弹幕，并在结果里写清原因。所以 `bili_watch` 在任何情况下都比 `danmaku`
只多不少。

## 链接怎么认（`links.py`）

`bili_watch` 和别的入口都支持这几种写法：

| 输入 | 处理 |
|---|---|
| `BV11SJt63ERg` | 直接用 |
| `https://www.bilibili.com/video/BV11SJt63ERg/?spm_id_from=...` | 抠出 BV 号 |
| `https://b23.tv/AbCdEf1` | **跟一次 302 重定向**再抠 BV 号（要联网） |
| `av170001` | 原样交给上层换 BV |

认不出来会明确说「认不出这是哪个视频」，不会瞎猜。

## 三个工具

## 自检

用实例自带 venv（别用系统 python）：

```powershell
$env:NEO_ROOT="F:\Neo-MoFox-Aemeath"

# 1) 导入冒烟：证明框架能加载
& "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" tests\check_import.py

# 2) 端到端：真装一遍、真查一次（只读，写操作只验证被拦）
& "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" tests\check_logic.py
```

两个脚本都按「失败项计数 → exit 1」收尾，方便进 CI。

## 实测踩过的坑（别再踩回去）

1. **`description` 里的转义引号**：写成 `\\"` 会让字符串提前断开（`\\` 是字面反斜杠，
   后面的 `"` 就结束了）。工具描述里要用引号，用中文「」最省事。
2. **cookie 路径推导**：早期写死「往上三级」，真机一跑找到的是工作区根而不是实例根。
   现在认 `NEO_ROOT` → 向上找 `main.py` → 兜底，三级这个数字千万别再写回来。
3. **B 站 deflate 是 raw 的**：`Content-Encoding: deflate` 但内容没有 0x78 zlib 头，
   `zlib.decompress` 默认解不开（异常一吞，弹幕就变成 0 条）。要两种 wbits 都试。
4. **`BaseTool.__init__(plugin)`**：工具实例化必须传 plugin 实例，工具层靠
   `self.plugin.config` 读配置。
5. **弹幕字段叫 `msg` 不是 `message`**：发弹幕接口用错字段会被风控回 `36701`
   「弹幕包含被禁止的内容」——那其实不是内容问题。
6. **CDP 握手 403**：Chrome 111+ 会校验 WebSocket 握手的 Origin，启动参数里必须带
   `--remote-allow-origins=*`，否则 `websocket-client` 直接 `Handshake status 403 Forbidden`。
7. **profile 单例锁**：Chrome 被强杀后留下 `SingletonLock` / `SingletonCookie` /
   `SingletonSocket`，下次用同一个 user-data-dir 启动会「启动后立刻退出（退出码 0）」——
   它以为已有实例、把活转交过去就退了。启动前清锁，且 `start()` 失败时**必须**收进程
   （`__enter__` 抛异常时 `__exit__` 不会跑，这是第一版留下 9 个僵尸 Chrome 的原因）。
8. **`--start-minimized` 压不住抢焦点**：实测窗口还是会抢一次前台。要叠加三道：
   进程级 `STARTUPINFO.wShowWindow = SW_MINIMIZE`、窗口坐标丢屏幕外、
   连上 CDP 后 `Browser.setWindowBounds` 再压一次。
9. **未登录只能试看 30 秒**：插件用的独立 profile 是空的，必须在导航前往里
   `Network.setCookie` 注入登录态，否则高清和完整时长都没有。
10. **视觉模型要 `init_model_config` 先跑**：框架的 `get_model_set_by_task()` 依赖
    `init_model_config("config/model.toml")`（`bot.py:319` 干的活）。自检脚本里不补这一步，
    就会以「Model config not initialized」静默降级成「只给截图路径」——
    看着像跑通了，其实画面描述根本没发生。
