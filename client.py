"""B 站 HTTP 客户端：纯标准库，不依赖 requests / httpx / 浏览器。

设计要点
--------
- **同步实现**：工具层用 ``asyncio.to_thread`` 丢线程池，避免在事件循环里做阻塞 IO；
- **登录态**：只读一份扁平的 cookies 字典（SESSDATA / bili_jct / DedeUserID ...），
  格式与 ``bili-tools/cookies.json`` 一致——那份文件由 Playwright 的
  ``Network.getAllCookies`` 一次性导出（httpOnly 的 SESSDATA 也在里面）；
- **wbi 签名**：B 站多数写接口要 ``w_rid``/``wts``，这里照官方算法实现
  （``wbi_img`` 取两段文件名 → 43 位置换表 → md5），key 缓存一小时；
- **错误统一**：所有接口返回 ``(ok, payload)``，``payload`` 要么是 dict 要么是错误文本，
  由工具层转成给模型看的中文说明。

本模块不 import 框架任何东西，方便单测与复用。
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

MIXIN_TAB: list[int] = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

MAIN_REFERER = "https://www.bilibili.com/"
MALL_REFERER = "https://mall.bilibili.com/"
MESSAGE_REFERER = "https://message.bilibili.com/"
SEARCH_REFERER = "https://search.bilibili.com/"
SPACE_REFERER = "https://space.bilibili.com/"

#: 关系状态码 → 中文（``x/relation`` 的 attribute）
RELATION_NAMES = {0: "未关注", 1: "悄悄关注", 2: "已关注", 6: "互关", 128: "已拉黑"}


class BiliError(Exception):
    """客户端层的显式错误（登录态缺失、网络失败、接口返回非 0）。"""


def dm_text(content: str, msg_type: int) -> str:
    """把私信 ``content``（JSON 字符串）转成给人看的一句话。

    私信接口的 content 永远是一段 JSON 字符串，不同 ``msg_type`` 结构完全不同：

    - ``1`` 文本 → ``{"content": "正文"}``
    - ``2`` 图片 → ``{"url": "..."}``
    - ``5`` 撤回 → 空对象
    - ``10`` 卡片（系统通知 / 分享） → ``{"title": ..., "text": ..., "jump_text": ...}``
    - ``7`` 分享 → ``{"title": ..., "desc": ..., "jump_url": ...}``

    解析失败就原样截断——宁可给模型看一段短原文，也不要抛异常把整轮查询打断。
    """

    raw = (content or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]
    if not isinstance(data, dict):
        return str(data)[:200]

    if msg_type == 1:
        return str(data.get("content") or "")[:500]
    if msg_type == 2:
        url = str(data.get("url") or "")
        return f"[图片] {url}".strip() if url else "[图片]"
    if msg_type == 5:
        return "[撤回了一条消息]"

    pieces: list[str] = []
    for key in ("title", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            pieces.append(value.strip())
    if pieces:
        return " ".join(pieces)[:300]

    for key in ("content", "desc", "summary", "jump_text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return f"[消息类型 {msg_type}]"


def load_cookies(path: str | Path) -> dict[str, str]:
    """读取 cookies.json，返回扁平的 name→value 字典。"""

    p = Path(path).expanduser()
    if not p.is_file():
        raise BiliError(f"找不到登录态文件：{p}")

    raw = json.loads(p.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict):
        if all(isinstance(v, (str, int, float)) for v in raw.values()):
            return {str(k): str(v) for k, v in raw.items()}
        # 兼容 [{name, value}, ...] 形式
        if isinstance(raw.get("cookies"), list):
            raw = raw["cookies"]
        else:
            raise BiliError("登录态文件格式无法识别（既不是扁平字典，也不是 cookies 列表）")

    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = str(item.get("value", ""))
        if out:
            return out
    raise BiliError("登录态文件格式无法识别")


class BiliClient:
    """一个账号一个实例；线程安全（无共享可变状态，wbi key 只做一次性缓存）。"""

    def __init__(self, cookies: dict[str, str], timeout: float = 15.0) -> None:
        if not cookies.get("SESSDATA"):
            raise BiliError("登录态里没有 SESSDATA，需要重新从 Playwright 导出一次")
        self.cookies = dict(cookies)
        self.timeout = timeout
        self.uid = str(cookies.get("DedeUserID") or "")
        self.csrf = str(cookies.get("bili_jct") or "")
        self._wbi_key: str | None = None
        self._wbi_at: float = 0.0

    # ------------------------------------------------------------------ 基础

    @property
    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _open(self, url: str, data: bytes | None = None, *, referer: str = MAIN_REFERER) -> bytes:
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        req.add_header("Origin", referer.rstrip("/"))
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Accept-Encoding", "identity")
        req.add_header("Cookie", self._cookie_header)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也尽量读正文
            body = exc.read() or b""
            enc = ""
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise BiliError(f"网络请求失败：{exc}") from exc

        if enc == "gzip":
            try:
                body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
            except zlib.error:
                pass
        elif enc == "deflate":
            # B 站的 deflate 常常是 **raw** 的（没有 0x78 zlib 头）：
            # 直接 zlib.decompress(默认 wbits) 会抛错，然后压缩流被当文本用，
            # 弹幕就变成 0 条——这是实测踩出来的坑，两种都要试。
            for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                try:
                    body = zlib.decompress(body, wbits)
                    break
                except zlib.error:
                    continue
        return body

    def _json(self, url: str, data: bytes | None = None, *, referer: str = MAIN_REFERER) -> dict[str, Any]:
        raw = self._open(url, data, referer=referer)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise BiliError("接口返回空响应")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            head = text[:80].replace("\n", " ")
            raise BiliError(f"接口返回的不是 JSON（前 80 字：{head}）") from exc
        if not isinstance(parsed, dict):
            raise BiliError("接口返回的 JSON 不是对象")
        return parsed

    def get(self, url: str, *, referer: str = MAIN_REFERER) -> dict[str, Any]:
        return self._json(url, None, referer=referer)

    def post(self, url: str, form: dict[str, Any], *, referer: str = MAIN_REFERER) -> dict[str, Any]:
        data = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
        return self._json(url, data, referer=referer)

    # -------------------------------------------------------------- wbi 签名

    def _signed(self, params: dict[str, Any]) -> dict[str, Any]:
        key = self._mixin_key()
        stamp = int(time.time())
        merged = {k: str(v).replace("!'()*", "") for k, v in params.items()}
        merged["wts"] = str(stamp)
        query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(merged[k], safe='')}"
            for k in sorted(merged)
        )
        merged["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
        return merged

    def _mixin_key(self) -> str:
        if self._wbi_key and time.time() - self._wbi_at < 3600:
            return self._wbi_key
        nav = self.get("https://api.bilibili.com/x/web-interface/nav")
        data = nav.get("data") or {}
        img = data.get("wbi_img") or {}
        names = []
        for url in (img.get("img_url") or "", img.get("sub_url") or ""):
            names.append(str(url).split("/")[-1].split(".")[0])
        raw = "".join(names)
        if len(raw) < 64:
            raise BiliError("拿不到 wbi 签名素材（nav 接口异常），登录态可能已失效")
        self._wbi_key = "".join(raw[i] for i in MIXIN_TAB)[:32]
        self._wbi_at = time.time()
        return self._wbi_key

    def post_signed(self, url: str, params: dict[str, Any], *, referer: str = MAIN_REFERER) -> dict[str, Any]:
        form = self._signed({**params, "csrf": self.csrf})
        return self.post(url, form, referer=referer)

    def get_signed(self, url: str, params: dict[str, Any], *, referer: str = MAIN_REFERER) -> dict[str, Any]:
        """带 wbi 签名的 GET（搜索用户、空间信息这类接口要 w_rid/wts）。"""

        signed = self._signed(params)
        separator = "&" if "?" in url else "?"
        return self.get(f"{url}{separator}{urllib.parse.urlencode(signed)}", referer=referer)

    # ------------------------------------------------------------------- 读

    def nav(self) -> dict[str, Any]:
        res = self.get("https://api.bilibili.com/x/web-interface/nav")
        if res.get("code") != 0:
            raise BiliError(f"读取账号信息失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        info = data.get("level_info") or {}
        return {
            "mid": data.get("mid"),
            "昵称": data.get("uname"),
            "等级": info.get("current_level"),
            "硬币": data.get("money"),
            "大会员": data.get("vipStatus") == 1,
        }

    def unread(self) -> dict[str, Any]:
        res = self.get("https://api.bilibili.com/x/msgfeed/unread")
        if res.get("code") != 0:
            raise BiliError(f"读取未读失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        keep = {"like", "reply", "at", "chat", "danmu", "sys_msg", "favorite", "coin"}
        return {k: v for k, v in data.items() if k in keep}

    def video(self, bvid: str) -> dict[str, Any]:
        res = self.get(f"https://api.bilibili.com/x/web-interface/view?bvid={urllib.parse.quote(bvid)}")
        if res.get("code") != 0:
            raise BiliError(f"取视频失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        pages = data.get("pages") or [{}]
        stat = data.get("stat") or {}
        return {
            "aid": data.get("aid"),
            "cid": (pages[0] or {}).get("cid"),
            "标题": data.get("title"),
            "up": (data.get("owner") or {}).get("name"),
            "时长秒": data.get("duration"),
            "播放": stat.get("view"),
            "点赞": stat.get("like"),
            "弹幕": stat.get("danmaku"),
            "评论": stat.get("reply"),
            "简介": (data.get("desc") or "")[:200],
        }

    def danmaku(self, bvid: str, limit: int = 40) -> dict[str, Any]:
        info = self.video(bvid)
        cid = info.get("cid")
        if not cid:
            raise BiliError("这个视频拿不到 cid，可能已失效")
        raw = self._open(f"https://comment.bilibili.com/{cid}.xml", referer=MAIN_REFERER)
        text = raw.decode("utf-8", errors="replace")
        import re

        items = []
        for match in re.finditer(r'<d p="([^"]*)">([^<]*)</d>', text):
            stamp = match.group(1).split(",")[0]
            try:
                at = float(stamp)
            except ValueError:
                at = 0.0
            body = (
                match.group(2)
                .replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
            )
            items.append({"t": at, "text": body})
        total = len(items)
        if limit and total > limit:
            step = max(1, total // limit)
            items = items[::step][:limit]
        return {"标题": info.get("标题"), "弹幕总数": total, "取样": items}

    def orders(self, page: int = 0, todo_only: bool = False) -> dict[str, Any]:
        pages = [0, 1] if todo_only else [page]
        collected: list[dict[str, Any]] = []
        total = 0
        for idx in pages:
            stamp = int(time.time() * 1000)
            res = self.get(
                "https://show.bilibili.com/api/ticket/ordercenter/list"
                f"?pageNum={idx}&pageSize=20&customer=0&platform=h5&build=0&v={stamp}&order_type",
                referer=MALL_REFERER,
            )
            if res.get("code") != 0:
                raise BiliError(f"取订单失败：{res.get('message') or res.get('code')}")
            data = res.get("data") or {}
            total = data.get("total") or total
            for order in data.get("list") or []:
                names = [r.get("name") for r in (order.get("rows") or []) if r.get("name")]
                collected.append({
                    "订单号": str(order.get("order_id")),
                    "商品": (" + ".join(names) or str(order.get("item_name_desc") or ""))[:50],
                    "店铺": order.get("shop_name"),
                    "实付": round((order.get("pay_money") or 0) / 100, 2),
                    "状态": str(order.get("status_name") or "") + (
                        "/" + str(order.get("status_subname")) if order.get("status_subname") else ""
                    ),
                    "时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(order.get("order_ctime") or 0)),
                })
        if todo_only:
            collected = [o for o in collected if "已完成" not in o["状态"] and "已关闭" not in o["状态"]]
        return {"总数": total, "筛选": "待办" if todo_only else "全部", "订单": collected}

    def order(self, order_id: str) -> dict[str, Any]:
        stamp = int(time.time() * 1000)
        res = self.get(
            f"https://mall.bilibili.com/mall-c/order/detail?mVersion=116&orderId={order_id}"
            f"&platform=h5&resellPay=&time={stamp}",
            referer=MALL_REFERER,
        )
        if res.get("code") != 0:
            raise BiliError(f"取订单详情失败：{res.get('message') or res.get('code')}")
        vo = (res.get("data") or {}).get("vo") or {}
        basic = vo.get("orderBasic") or {}
        express = vo.get("orderExpress") or {}
        deliver = vo.get("orderDeliver") or {}
        ext = vo.get("extData") or {}
        return {
            "订单号": str(basic.get("orderId") or order_id),
            "状态": basic.get("orderStatusTitle"),
            "提示": basic.get("unReceiptMessage") or basic.get("orderStatusDesc"),
            "商品": [
                {
                    "名称": sku.get("itemsName"),
                    "规格": sku.get("skuSpec"),
                    "单价": sku.get("price"),
                    "数量": sku.get("skuNum"),
                }
                for sku in (vo.get("skuList") or [])
            ],
            "实付": basic.get("realPayMoney"),
            "运费": basic.get("expressTotalMoney"),
            "尾款": {
                "定金": ext.get("frontMoney"),
                "尾款": ext.get("finalMoney"),
                "截止": (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(ext["finalMoneyEnd"] / 1000))
                    if ext.get("finalMoneyEnd") else None
                ),
            },
            "快递": {"公司": express.get("com_v"), "单号": express.get("sno")},
            "收货": {
                "姓名": deliver.get("deliverName"),
                "地址": deliver.get("deliverAddr"),
            },
            "可操作": [op.get("name") for op in (vo.get("op_json") or [])],
        }

    def history(self, limit: int = 12) -> dict[str, Any]:
        res = self.get(
            "https://api.bilibili.com/x/web-interface/history/cursor?max=0&view_at=0&business=&ps="
            f"{max(1, min(limit, 30))}"
        )
        if res.get("code") != 0:
            raise BiliError(f"取历史失败：{res.get('message') or res.get('code')}")
        rows = (res.get("data") or {}).get("list") or []
        return {
            "条数": len(rows),
            "历史": [
                {
                    "标题": (row.get("title") or "")[:36],
                    "up": (row.get("owner") or {}).get("name"),
                    "进度": f"{round(row.get('progress') or 0)}s" if (row.get("progress") or 0) >= 0 else "看完",
                }
                for row in rows
            ],
        }

    def favs(self) -> dict[str, Any]:
        res = self.get(
            f"https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={self.uid}"
        )
        if res.get("code") != 0:
            raise BiliError(f"取收藏夹失败：{res.get('message') or res.get('code')}")
        folders = [
            {"id": f.get("id"), "名称": f.get("title"), "数量": f.get("media_count")}
            for f in ((res.get("data") or {}).get("list") or [])
        ]
        return {"收藏夹": folders}

    def followings(self, page: int = 1) -> dict[str, Any]:
        res = self.get(
            f"https://api.bilibili.com/x/relation/followings?vmid={self.uid}&pn={page}"
            "&ps=20&order=desc&order_type=attention"
        )
        if res.get("code") != 0:
            raise BiliError(f"取关注失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        return {
            "关注总数": data.get("total"),
            "本页": [
                {"mid": x.get("mid"), "昵称": x.get("uname")} for x in (data.get("list") or [])
            ],
        }

    # --------------------------------------------------------------- 私信

    def chat_sessions(self, size: int = 20, page: int = 1) -> dict[str, Any]:
        """私信会话列表（最近的在前）。

        注意两个坑：

        - 普通用户会话的 ``last_msg.account_info`` 是空的，昵称要另外批量查
          （:meth:`chat_peer_names`），别指望这里有名字；
        - 系统通知会话的 ``system_msg_type`` 非 0（talker_id 是 8.4e14 那种大数），
          它们不是「谁给你发私信」，巡查时要排除。
        """

        res = self.get(
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
            "?session_type=1&group_id=0&build=0&mobi_app=web&platform=web"
            f"&web_location=333.1387&end_seqno=&pn={page}&ps={max(1, size)}",
            referer=MESSAGE_REFERER,
        )
        if res.get("code") != 0:
            raise BiliError(f"取私信会话失败：{res.get('message') or res.get('code')}")
        raw = (res.get("data") or {}).get("session_list") or []
        my_uid = int(self.uid or 0)
        sessions: list[dict[str, Any]] = []
        for item in raw:
            last = item.get("last_msg") or {}
            talker = int(item.get("talker_id") or 0)
            sender = int(last.get("sender_uid") or 0)
            account = last.get("account_info") or {}
            msg_type = int(last.get("msg_type") or 0)
            sessions.append({
                "talker_id": talker,
                "系统": bool(item.get("system_msg_type")) or talker <= 0,
                "unread": int(item.get("unread_count") or 0),
                "last_ts": int(last.get("timestamp") or item.get("session_ts") or 0),
                "last_sender_uid": sender,
                "我发的": sender == my_uid,
                "昵称": str(account.get("name") or ""),
                "消息类型": msg_type,
                "摘要": dm_text(str(last.get("content") or ""), msg_type),
            })
        return {"会话": sessions, "总数": len(sessions)}

    def chat_peer_names(self, uids: list[int]) -> dict[int, str]:
        """批量查用户昵称（会话列表里没有名字，靠这个接口补）。"""

        ids = [str(int(u)) for u in uids if int(u or 0) > 0]
        if not ids:
            return {}
        res = self.get(
            "https://api.vc.bilibili.com/account/v1/user/cards?uids=" + ",".join(ids),
            referer=MESSAGE_REFERER,
        )
        if res.get("code") != 0:
            return {}
        data = res.get("data") or []
        if not isinstance(data, list):
            return {}
        return {
            int(card.get("mid")): str(card.get("name") or "")
            for card in data
            if isinstance(card, dict) and card.get("mid")
        }

    def chat_messages(self, talker_id: int, size: int = 20) -> dict[str, Any]:
        """拉某个会话最近的消息（按时间正序返回）。"""

        res = self.get(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
            f"?session_type=1&talker_id={int(talker_id)}&session_id=0&size={max(1, size)}"
            "&build=0&mobi_app=web&platform=web&web_location=333.1387",
            referer=MESSAGE_REFERER,
        )
        if res.get("code") != 0:
            raise BiliError(f"取私信内容失败：{res.get('message') or res.get('code')}")
        raw = (res.get("data") or {}).get("messages") or []
        my_uid = int(self.uid or 0)
        messages: list[dict[str, Any]] = []
        for msg in reversed(raw):  # 接口给的是新→旧，这里翻成正序方便阅读
            sender = int(msg.get("sender_uid") or 0)
            msg_type = int(msg.get("msg_type") or 0)
            messages.append({
                "时间": time.strftime(
                    "%m-%d %H:%M", time.localtime(int(msg.get("timestamp") or 0))
                ),
                "时间戳": int(msg.get("timestamp") or 0),
                "我": sender == my_uid,
                "类型": msg_type,
                "文本": dm_text(str(msg.get("content") or ""), msg_type),
            })
        return {"消息": messages, "条数": len(messages)}

    # --------------------------------------------------------------- 用户

    def search_users(self, keyword: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """搜 UP 主 / 用户（返回候选列表，不替调用方做选择）。"""

        res = self.get_signed(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            {
                "keyword": keyword,
                "search_type": "bili_user",
                "page": max(1, page),
                "page_size": max(1, page_size),
            },
            referer=SEARCH_REFERER,
        )
        if res.get("code") != 0:
            raise BiliError(f"搜索用户失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        candidates: list[dict[str, Any]] = []
        for row in data.get("result") or []:
            verify = row.get("official_verify") or {}
            candidates.append({
                "mid": int(row.get("mid") or 0),
                "昵称": str(row.get("uname") or ""),
                "粉丝": int(row.get("fans") or 0),
                "投稿": int(row.get("videos") or 0),
                "等级": int(row.get("level") or 0),
                "认证": str(verify.get("desc") or ""),
                "签名": str(row.get("usign") or "")[:80],
            })
        return {
            "关键词": keyword,
            "总数": data.get("numResults"),
            "候选": candidates,
        }

    def relation(self, mid: int) -> dict[str, Any]:
        """查和某个用户的关系（是否已关注）。"""

        res = self.get(
            f"https://api.bilibili.com/x/relation?fid={int(mid)}", referer=SPACE_REFERER
        )
        if res.get("code") != 0:
            raise BiliError(f"查关注状态失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        attribute = data.get("attribute")
        return {
            "attribute": attribute,
            "状态": RELATION_NAMES.get(attribute, f"未知({attribute})"),
            "关注了": attribute in (2, 6),
            "互关": attribute == 6,
        }

    # ------------------------------------------------------------------- 写

    def reply(self, bvid: str, text: str) -> dict[str, Any]:
        info = self.video(bvid)
        res = self.post_signed(
            "https://api.bilibili.com/x/v2/reply/add",
            {
                "oid": info["aid"],
                "type": 1,
                "message": text,
                "plat": 1,
                "ordering": "heat",
                "gaia_source": "main_web",
            },
        )
        if res.get("code") != 0:
            raise BiliError(f"发评论失败：{res.get('message') or res.get('code')}")
        data = res.get("data") or {}
        return {"rpid": data.get("rpid"), "视频": info.get("标题")}

    def send_danmaku(self, bvid: str, text: str, progress: int = 5) -> dict[str, Any]:
        info = self.video(bvid)
        res = self.post_signed(
            "https://api.bilibili.com/x/v2/dm/post",
            {
                "oid": info["cid"],
                "type": 1,
                "msg": text,
                "progress": max(1, progress) * 1000,
                "mode": 1,
                "fontsize": 25,
                "color": 16777215,
                "pool": 0,
                "plat": 1,
                "aid": info["aid"],
                "rnd": 2,
                "checkbox_type": 0,
                "gaiasource": "main_web",
                "polaris_app_id": 100,
                "polaris_platform": 5,
            },
        )
        if res.get("code") != 0:
            raise BiliError(f"发弹幕失败：{res.get('message') or res.get('code')}")
        return {"dmid": str((res.get("data") or {}).get("dmid_str") or ""), "视频": info.get("标题")}

    def like(self, bvid: str, on: bool = True) -> dict[str, Any]:
        info = self.video(bvid)
        res = self.post_signed(
            "https://api.bilibili.com/x/web-interface/archive/like",
            {"aid": info["aid"], "like": 1 if on else 2},
        )
        if res.get("code") != 0:
            raise BiliError(f"{'点赞' if on else '取消点赞'}失败：{res.get('message') or res.get('code')}")
        return {"动作": "点赞" if on else "取消点赞", "视频": info.get("标题")}

    def fav(self, bvid: str, folder_id: str, add: bool = True) -> dict[str, Any]:
        info = self.video(bvid)
        payload: dict[str, Any] = {"rid": info["aid"], "type": 2}
        if add:
            payload["add_media_ids"] = folder_id
        else:
            payload["del_media_ids"] = folder_id
        res = self.post_signed("https://api.bilibili.com/x/v3/fav/resource/deal", payload)
        if res.get("code") != 0:
            raise BiliError(f"{'收藏' if add else '取消收藏'}失败：{res.get('message') or res.get('code')}")
        return {"动作": "收藏" if add else "取消收藏", "视频": info.get("标题")}

    def follow(self, mid: int, on: bool = True) -> dict[str, Any]:
        """关注 / 取关某个 UP 主。

        接口不需要 wbi 签名，只要 csrf（来自 cookie 的 bili_jct）。

        幂等处理：关注一个已经关注的人会返回 ``22014``（已经关注用户），
        取关一个没关注的人同理——这两种都按「已经是目标状态」返回成功，
        免得模型把「重复关注」当成错误去瞎重试。
        """

        if not self.csrf:
            raise BiliError("登录态里没有 bili_jct（csrf），关注操作需要它——请重新导出一份 cookies")
        res = self.post(
            "https://api.bilibili.com/x/relation/modify",
            {
                "fid": int(mid),
                "act": 1 if on else 2,
                "re_src": 11,
                "csrf": self.csrf,
            },
            referer=SPACE_REFERER,
        )
        code = res.get("code")
        message = str(res.get("message") or "")
        if code != 0:
            already = on and code == 22014
            not_followed = (not on) and ("未关注" in message or "没有关注" in message)
            if already or not_followed:
                return {"动作": "关注" if on else "取关", "已存在": True, "提示": message}
            raise BiliError(f"{'关注' if on else '取关'}失败：{message or code}")
        return {"动作": "关注" if on else "取关", "已存在": False, "提示": message}


__all__ = ["BiliClient", "BiliError", "load_cookies", "dm_text", "MIXIN_TAB", "RELATION_NAMES"]
