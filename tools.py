"""两个工具：``bili_query``（只读）与 ``bili_action``（写，要开开关）。

为什么拆成两个：只读永远不会出问题，写操作会挂在主人账号上。拆开之后，
配置里可以把写闸门关死（默认就是关的），而查询照样随时可用。

返回给模型的都是**短中文文本**，不是整段 JSON——B 站接口字段又长又多（一个订单
列表能有几万字），全塞进上下文纯属烧 token。要细节就再单独查那一项。

⚠️ 防背书：工具返回值也会进上下文。每个返回末尾都带一句「这是系统回执，不用念出来」，
否则模型容易照着念一遍（三合一指导 §11.8 的坑）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from src.app.plugin_system.base import BaseTool

from . import runtime, vision
from .client import BiliError

RECEIPT_HINT = "\n（这是系统回执，不用念出来，也不用向任何人报告。）"

NO_CLIENT = (
    "现在没法操作 B 站：插件启动时没读到登录态。\n"
    "请让主人确认 config/plugins/bili_whale/config.toml 里的 plugin.cookie_file 指向的"
    " cookies.json 存在、且至少含 SESSDATA / bili_jct / DedeUserID。"
    "登录态要用 playwright MCP 打开 B 站导出一份新的。"
)


def _client_or_error():
    client = runtime.get_client()
    if client is None:
        detail = runtime.login_error()
        return None, NO_CLIENT + (f"\n（上次失败原因：{detail}）" if detail else "")
    return client, ""


async def _run(func, *args, **kwargs) -> dict[str, Any]:
    """把同步 HTTP 调用丢进线程池，别堵事件循环。"""

    return await asyncio.to_thread(func, *args, **kwargs)


# --------------------------------------------------------------------- 只读


class BiliQueryTool(BaseTool):
    """查询 B 站账号与视频信息（只读，不改任何东西）。"""

    name: str = "bili_query"
    description: str = (
        "查询主人在 B 站上的信息，只读。用 action 选查什么：\n"
        "- account：账号概况（昵称/等级/硬币/大会员）；\n"
        "- unread：未读消息计数（点赞、回复、@、私信、弹幕、系统通知）；\n"
        "- orders：会员购订单列表（target 写 todo 就只看未完成的）；\n"
        "- order：单个订单详情，target 填订单号（例：「8001220220438018」）——"
        "会给商品、实付、定金尾款与截止、快递单号、收货信息、当前可做的操作；\n"
        "- video：视频信息，target 填 BV 号；\n"
        "- danmaku：视频弹幕，target 填 BV 号——这是「看懂一个视频在讲什么」最省钱的方式，"
        "弹幕按时间轴铺开，观众态度、UP 主被挑的错都藏在里面；\n"
        "- history：主人最近看过什么；\n"
        "- favs：收藏夹列表；\n"
        "- followings：关注的人（第 limit 页，每页 20）。\n"
        "这些都是只读操作，随时可以调。查完直接用人话回答主人，不要复述原始字段。"
    )

    async def execute(
        self,
        action: Annotated[str, "要查什么：account/unread/orders/order/video/danmaku/history/favs/followings"],
        target: Annotated[str, "目标：BV 号、订单号、todo（orders 专用）；不需要就留空"] = "",
        limit: Annotated[int, "条数或页码，0 表示用插件配置的默认值"] = 0,
    ) -> tuple[bool, str]:
        """执行一次只读查询。"""

        client, err = _client_or_error()
        if client is None:
            return False, err

        act = (action or "").strip().lower()
        arg = (target or "").strip()
        config = getattr(self, "plugin", None)
        query_cfg = getattr(config, "config", None)
        default_history = 12
        default_danmaku = 40
        order_todo_default = False
        try:
            if query_cfg is not None:
                default_history = int(query_cfg.query.history_limit)
                default_danmaku = int(query_cfg.query.danmaku_limit)
                order_todo_default = bool(query_cfg.query.order_todo_only)
        except Exception:  # noqa: BLE001 - 配置读不到就用内置默认
            pass

        try:
            if act == "account":
                data = await _run(client.nav)
                return True, (
                    f"{data['昵称']}（Lv{data['等级']}），硬币 {data['硬币']}，"
                    f"{'大会员' if data['大会员'] else '非大会员'}。{RECEIPT_HINT}"
                )

            if act == "unread":
                data = await _run(client.unread)
                if not any(data.values()):
                    return True, f"没有未读消息。{RECEIPT_HINT}"
                names = {
                    "like": "点赞", "reply": "评论回复", "at": "@我", "chat": "私信",
                    "danmu": "弹幕", "sys_msg": "系统通知", "favorite": "收藏", "coin": "投币",
                }
                parts = [f"{names.get(k, k)} {v}" for k, v in data.items() if v]
                return True, f"未读：{'、'.join(parts)}。{RECEIPT_HINT}"

            if act == "orders":
                todo = order_todo_default or arg.lower() in ("todo", "待办", "未完成")
                data = await _run(client.orders, 0, todo)
                rows = data["订单"]
                if not rows:
                    return True, f"会员购共 {data['总数']} 单，当前筛选（{data['筛选']}）没有匹配的。{RECEIPT_HINT}"
                lines = [f"会员购共 {data['总数']} 单，{data['筛选']} {len(rows)} 条："]
                for row in rows[:12]:
                    lines.append(
                        f"· {row['订单号']} {row['状态']} ¥{row['实付']} — {row['商品']}（{row['时间']}）"
                    )
                if len(rows) > 12:
                    lines.append(f"…还有 {len(rows) - 12} 条，需要细看就查具体订单号。")
                return True, "\n".join(lines) + RECEIPT_HINT

            if act == "order":
                if not arg:
                    return False, "要查单个订单得告诉我订单号（target 里填）。"
                data = await _run(client.order, arg)
                lines = [f"订单 {data['订单号']}：{data['状态']}"]
                if data.get("提示"):
                    lines.append(f"提示：{data['提示']}")
                for sku in data.get("商品") or []:
                    lines.append(f"商品：{sku['名称']} {sku.get('规格') or ''} ×{sku.get('数量')}")
                lines.append(f"实付 ¥{data['实付']}")
                final = data.get("尾款") or {}
                if final.get("尾款"):
                    lines.append(f"定金 ¥{final.get('定金')} + 尾款 ¥{final.get('尾款')}（截止 {final.get('截止')}）")
                exp = data.get("快递") or {}
                if exp.get("单号"):
                    lines.append(f"快递：{exp.get('公司')} {exp.get('单号')}")
                addr = (data.get("收货") or {}).get("地址")
                if addr:
                    lines.append(f"收货：{data.get('收货', {}).get('姓名')} · {addr}")
                if data.get("可操作"):
                    lines.append(f"可操作：{'、'.join(data['可操作'])}")
                return True, "\n".join(lines) + RECEIPT_HINT

            if act == "video":
                if not arg:
                    return False, "要看视频信息得给我 BV 号。"
                data = await _run(client.video, arg)
                return True, (
                    f"《{data['标题']}》 UP：{data['up']}，{data['时长秒']}秒，"
                    f"播放 {data['播放']} / 点赞 {data['点赞']} / 弹幕 {data['弹幕']} / 评论 {data['评论']}。"
                    f"简介：{data['简介']}{RECEIPT_HINT}"
                )

            if act == "danmaku":
                if not arg:
                    return False, "要抓弹幕得给我 BV 号。"
                count = limit or default_danmaku
                count = max(5, min(int(count), 200))
                data = await _run(client.danmaku, arg, count)
                lines = [f"《{data['标题']}》共 {data['弹幕总数']} 条弹幕，下面按时间取 {len(data['取样'])} 条："]
                seen: set[str] = set()
                for item in data["取样"]:
                    text = item["text"].strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    minutes = int(item["t"]) // 60
                    seconds = int(item["t"]) % 60
                    lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
                return True, "\n".join(lines) + RECEIPT_HINT

            if act == "history":
                count = limit or default_history
                data = await _run(client.history, max(1, min(int(count), 30)))
                lines = [f"最近看了 {data['条数']} 条："]
                for row in data["历史"]:
                    lines.append(f"· {row['标题']}（{row['up']}，{row['进度']}）")
                return True, "\n".join(lines) + RECEIPT_HINT

            if act == "favs":
                data = await _run(client.favs)
                lines = ["收藏夹：" + "、".join(
                    f"{f['名称']}({f['数量']})" for f in data["收藏夹"]
                )]
                lines.append("（要往哪个夹子收藏，把那个 id 记下来：" + "、".join(
                    f"{f['名称']}={f['id']}" for f in data["收藏夹"]
                ) + "）")
                return True, "\n".join(lines) + RECEIPT_HINT

            if act == "followings":
                page = max(1, int(limit) or 1)
                data = await _run(client.followings, page)
                names = "、".join(x["昵称"] for x in data["本页"][:20])
                return True, f"关注 {data['关注总数']} 人，第 {page} 页：{names}。{RECEIPT_HINT}"

            return False, (
                f"不认识的查询类型：{action!r}。可选：account / unread / orders / order / "
                "video / danmaku / history / favs / followings。"
            )

        except BiliError as exc:
            return False, f"查询失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底不让工具炸掉整轮
            return False, f"查询异常（{type(exc).__name__}）：{exc}"


# --------------------------------------------------------------------- 写


class BiliActionTool(BaseTool):
    """在主人的 B 站账号上执行写操作（受配置开关约束）。"""

    name: str = "bili_action"
    description: str = (
        "替主人在 B 站上**动手**（会留下痕迹，受插件配置开关约束）。用 action 选做什么：\n"
        "- reply：给视频发评论，target 填 BV 号、text 填内容；"
        "内容里写 [表情名] 会自动变成那个表情；\n"
        "- danmaku：发弹幕，target 填 BV 号、text 填内容，limit 可给秒数（默认第 5 秒）——"
        "弹幕发出去删不掉；\n"
        "- like / unlike：点赞 / 取消点赞，target 填 BV 号；\n"
        "- fav_add / fav_del：收藏 / 取消收藏，target 填 BV 号、folder_id 填收藏夹 id（先用 favs 查）。\n"
        "**动之前先跟主人确认**：这是他自己账号，评论和弹幕发出去就收不回。"
        "被拒绝时说明配置里对应开关没开，把拒绝理由如实告诉主人，别自己找绕路。"
    )

    async def execute(
        self,
        action: Annotated[str, "写操作：reply/danmaku/like/unlike/fav_add/fav_del"],
        target: Annotated[str, "BV 号"] = "",
        text: Annotated[str, "评论或弹幕内容"] = "",
        folder_id: Annotated[str, "收藏夹 id（fav_add / fav_del 用）"] = "",
        limit: Annotated[int, "弹幕出现在第几秒，默认 5"] = 0,
    ) -> tuple[bool, str]:
        """执行一次写操作。"""

        client, err = _client_or_error()
        if client is None:
            return False, err

        config = getattr(self, "plugin", None)
        cfg = getattr(config, "config", None)
        enabled = False
        allow = {"reply": False, "danmaku": False, "like": True, "fav": True}
        try:
            if cfg is not None:
                enabled = bool(cfg.actions.enabled)
                allow = {
                    "reply": bool(cfg.actions.allow_reply),
                    "danmaku": bool(cfg.actions.allow_danmaku),
                    "like": bool(cfg.actions.allow_like),
                    "fav": bool(cfg.actions.allow_fav),
                }
        except Exception:  # noqa: BLE001
            enabled = False

        act = (action or "").strip().lower()
        bvid = (target or "").strip()

        if not enabled:
            return False, (
                "写操作现在是关着的（config/plugins/bili_whale/config.toml 里 "
                "[actions] enabled = false）。要动手得先让主人自己把总闸打开。"
            )
        if not bvid and act not in ("",):
            return False, "写操作要指定目标 BV 号。"

        try:
            if act == "reply":
                if not allow["reply"]:
                    return False, "配置里没开「允许发评论」（actions.allow_reply = false）。"
                if not (text or "").strip():
                    return False, "评论内容不能为空。"
                data = await _run(client.reply, bvid, text.strip())
                return True, f"评论发出去了，rpid={data['rpid']}（{data['视频']}）。{RECEIPT_HINT}"

            if act == "danmaku":
                if not allow["danmaku"]:
                    return False, "配置里没开「允许发弹幕」（actions.allow_danmaku = false）。"
                if not (text or "").strip():
                    return False, "弹幕内容不能为空。"
                seconds = max(1, int(limit) or 5)
                data = await _run(client.send_danmaku, bvid, text.strip(), seconds)
                return True, f"弹幕发出去了（第 {seconds} 秒），dmid={data['dmid']}（{data['视频']}）。{RECEIPT_HINT}"

            if act in ("like", "unlike"):
                if not allow["like"]:
                    return False, "配置里没开「允许点赞」（actions.allow_like = false）。"
                data = await _run(client.like, bvid, act == "like")
                return True, f"{data['动作']}成功（{data['视频']}）。{RECEIPT_HINT}"

            if act in ("fav_add", "fav_del"):
                if not allow["fav"]:
                    return False, "配置里没开「允许收藏」（actions.allow_fav = false）。"
                fid = (folder_id or "").strip()
                if not fid:
                    return False, "收藏要指定收藏夹 id（先用 bili_query 的 favs 查）。"
                data = await _run(client.fav, bvid, fid, act == "fav_add")
                return True, f"{data['动作']}成功（{data['视频']}）。{RECEIPT_HINT}"

            return False, (
                f"不认识的写操作：{action!r}。可选：reply / danmaku / like / unlike / fav_add / fav_del。"
            )

        except BiliError as exc:
            return False, f"操作失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"操作异常（{type(exc).__name__}）：{exc}"


# --------------------------------------------------------------------- 看视频


class BiliWatchTool(BaseTool):
    """看视频：信息 + 弹幕 + 若干帧真实画面。"""

    name: str = "bili_watch"
    description: str = (
        "看一个 B 站视频。target 可以是 BV 号、完整视频链接、或者 b23.tv 短链——都认。\n"
        "返回三样东西：视频信息（标题/UP/时长/播放）、按时间铺开的弹幕、"
        "以及按时间点抽的几张画面（截图存成文件，路径会列出来）。\n"
        "想知道「视频里到底讲了什么、画面长什么样」时用它；只看弹幕的话 bili_query 的 "
        "danmaku 更轻。\n"
        "它会起一个真实 Chrome（不是无头，避免风控）——窗口丢在屏幕外并最小化，"
        "不抢焦点、不挡主人干活，看完自己关掉。整段大约 5~15 秒。\n"
        "浏览器起不来时不会失败：照样回信息 + 弹幕，并说明原因。"
    )

    async def execute(
        self,
        target: Annotated[str, "BV 号 / 视频链接 / b23.tv 短链"],
        frames: Annotated[int, "抽几帧，0 表示用配置默认（一般 3）"] = 0,
    ) -> tuple[bool, str]:
        """打开视频并抽帧。"""

        client, err = _client_or_error()
        if client is None:
            return False, err

        cfg = getattr(getattr(self, "plugin", None), "config", None)
        if cfg is None:
            return False, "拿不到插件配置，没法起浏览器。"

        try:
            if not cfg.watch.enabled:
                return False, (
                    "看视频功能在配置里关着（[watch] enabled = false）。"
                    "现在只能回信息 + 弹幕，用 bili_query 的 video / danmaku。"
                )
        except Exception:  # noqa: BLE001
            pass

        from . import links, watch as watch_module

        root = getattr(self.plugin, "_framework_root", None)
        try:
            base = root() if callable(root) else None
        except Exception:  # noqa: BLE001
            base = None
        if base is None:
            return False, "定位不到框架根目录，没法放浏览器用户目录。"

        profile_dir = base / cfg.watch.browser_profile
        out_dir = base / cfg.watch.screenshot_dir
        want = int(frames or 0) or int(cfg.watch.frames or 3)

        try:
            result = await asyncio.to_thread(
                watch_module.watch,
                client,
                target,
                frames=want,
                profile_dir=profile_dir,
                out_dir=out_dir,
                danmaku_limit=int(cfg.query.danmaku_limit or 30),
                page_timeout=float(cfg.watch.page_timeout or 25.0),
            )
            # 抽到帧了就交给框架的视觉模型描述——「看懂画面」这一步在这里
            try:
                if cfg.watch.describe:
                    frames = [
                        (int(shot.get("秒") or 0), str(shot.get("图") or ""))
                        for shot in (result.get("抽帧") or [])
                    ]
                    description = await vision.describe_frames(
                        frames,
                        title=str((result.get("信息") or {}).get("标题") or ""),
                        task_name=str(cfg.watch.vision_task or "video"),
                        fallback_name=str(cfg.watch.vision_fallback or ""),
                    )
                    if description:
                        result["画面描述"] = description
            except Exception as exc:  # noqa: BLE001 - 描述失败不影响主结果
                result["描述错误"] = f"{type(exc).__name__}: {exc}"
            return True, watch_module.render(result) + RECEIPT_HINT
        except links.LinkError as exc:
            return False, f"没认出这个视频：{exc}"
        except BiliError as exc:
            return False, f"看视频失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"看视频异常（{type(exc).__name__}）：{exc}"


# --------------------------------------------------------------------- 目录


CATALOG = """B站能力目录（按分类。调用方式 = 工具名 + action + target）

【账号】只读
  bili_query  action=account                     昵称 / 等级 / 硬币 / 大会员
  bili_query  action=unread                      未读：点赞、回复、@、私信、弹幕
  bili_query  action=history   limit=条数         最近看过什么
  bili_query  action=favs                        收藏夹列表（带 id，收藏要用）
  bili_query  action=followings limit=页码        关注的人

【视频】只读
  bili_query  action=video    target=BV号        标题 / UP / 时长 / 播放 / 三连数 / 简介
  bili_query  action=danmaku  target=BV号        弹幕按时间铺开 ← 看懂视频最省钱的方式
  bili_watch  target=BV号/链接/b23.tv短链         真看：信息 + 弹幕 + 抽帧截图（会起屏幕外的 Chrome）

【订单】只读（会员购）
  bili_query  action=orders   target=todo        只看没完成的（待收货 / 待付款）
  bili_query  action=orders                      全部订单
  bili_query  action=order    target=订单号       商品 / 实付 / 定金尾款+截止 / 快递单号 / 收货信息

【互动】写操作，要配置放行、且先跟主人确认
  bili_action action=reply    target=BV号 text=内容          发评论（[表情名] 会变表情）
  bili_action action=danmaku  target=BV号 text=内容 limit=秒  发弹幕（删不掉）
  bili_action action=like|unlike  target=BV号                点赞 / 取消（可逆）
  bili_action action=fav_add|fav_del  target=BV号 folder_id=收藏夹id  收藏 / 取消（可逆）

查不到想做的事就说"这个插件没这块能力"，别硬猜 action。"""


class BiliCatalogTool(BaseTool):
    """B站能力目录：告诉她每件事该调哪个工具、哪个 action。"""

    name: str = "bili_catalog"
    description: str = (
        "查 B 站能力目录：把「查订单、看视频、发弹幕、点赞收藏」这些事按分类列全，"
        "并写明各自该用哪个工具、哪个 action、target 怎么填。\n"
        "第一次要碰 B 站相关的事、或者不确定某个动作该走哪个入口时，先调这个看一眼。\n"
        "纯静态清单，只读，不触碰账号，随时可以调。"
    )

    async def execute(self) -> tuple[bool, str]:
        """返回分类目录。"""

        return True, CATALOG + RECEIPT_HINT


__all__ = ["BiliQueryTool", "BiliActionTool", "BiliCatalogTool", "CATALOG"]
