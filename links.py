"""B 站链接解析：把主人随手发来的一串东西变成 BV 号。

实际会遇到的写法（都见过）：

- ``BV11SJt63ERg`` —— 裸 BV 号，最常见；
- ``https://www.bilibili.com/video/BV11SJt63ERg/?spm_id_from=...`` —— 分享出来的整条；
- ``https://b23.tv/AbCdEf1`` —— 手机分享的短链，**要跟一次重定向才知道是哪个视频**；
- ``av170001`` / ``https://www.bilibili.com/video/av170001`` —— 老 av 号。

短链解析要发一次 HTTP 请求（跟随 302），不带任何登录态——b23.tv 本来就不要求。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AV_RE = re.compile(r"(?<![A-Za-z])av(\d{1,12})", re.IGNORECASE)
SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/[^\s\"'<>，。]+", re.IGNORECASE
)


class LinkError(Exception):
    """链接认不出来 / 短链解析失败。"""


def _follow_short(url: str, timeout: float = 10.0) -> str:
    """跟随 b23.tv 的 302，拿到最终地址。"""

    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except urllib.error.URLError as exc:
        raise LinkError(f"短链解析失败（{url}）：{exc}") from exc


def extract_bvid(text: str) -> str | None:
    """从任意文本里抠出 BV 号。

    处理顺序：裸 BV → 视频整链 → av 号 → b23 短链（要联网）。
    认不出来返回 ``None``（调用方决定怎么提示）。
    """

    if not text:
        return None
    raw = str(text).strip()

    # 1) 直接就是/包含 BV 号
    hit = BV_RE.search(raw)
    if hit:
        return hit.group(0)

    # 2) b23.tv 短链 —— 跟一次重定向
    short = SHORT_RE.search(raw)
    if short:
        final = _follow_short(short.group(0))
        hit = BV_RE.search(final)
        if hit:
            return hit.group(0)
        av = AV_RE.search(final)
        if av:
            return f"av{av.group(1)}"
        raise LinkError(f"短链跳到了 {final}，但里面没有 BV 号")

    # 3) av 号
    av = AV_RE.search(raw)
    if av:
        return f"av{av.group(1)}"

    return None


def normalize_bvid(token: str) -> str:
    """把 ``av170001`` 之类的写法留给调用方换成 BV（这里只做格式校验）。

    不在这里发请求：转换需要 aid 接口，交给 client 层统一处理，省得两处各写一套。
    """

    token = str(token or "").strip()
    if BV_RE.fullmatch(token):
        return token
    return token


def looks_like_bili_link(text: str) -> bool:
    """快速判断这段文本里有没有 B 站链接/号（不联网）。"""

    raw = str(text or "")
    return bool(
        BV_RE.search(raw)
        or SHORT_RE.search(raw)
        or VIDEO_URL_RE.search(raw)
        or AV_RE.search(raw)
    )


__all__ = [
    "extract_bvid",
    "normalize_bvid",
    "looks_like_bili_link",
    "LinkError",
    "BV_RE",
    "SHORT_RE",
]
