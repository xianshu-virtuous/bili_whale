"""看视频：链接 → 有头浏览器播放 → 按时间点抽帧 → 打包成一段可读文本。

分工：纯 HTTP 负责拿信息（快、稳、不吃浏览器开销），浏览器只负责**画面**——
也就是主人真正在意的「视频里到底长什么样」。弹幕则永远走 HTTP，因为它比画面
更能说明「讲了什么、哪里被挑刺」。

失败降级是设计的一部分：浏览器起不来 / 没装 websocket-client / 被风控挡住，
都不该让整次「看视频」失败——降级成「信息 + 弹幕」照样能说明问题，同时把原因
如实写进结果里。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .browser import BrowserError, HeadfulChrome, find_browser
from .client import BiliClient, BiliError
from .links import LinkError, extract_bvid


def _js_str(value: str) -> str:
    """把字符串安全地塞进 JS 表达式。"""

    return json.dumps(value, ensure_ascii=False)


def frame_count_for(duration: int, requested: int = 0) -> int:
    """按时长决定抽几帧。

    固定抽 3 帧对短视频是浪费（2 分钟的片子 3 帧就够）、对长视频又不够
    （一小时播客 3 帧等于没看）。所以按长度给一个基准，用户/配置可以覆盖。

    **帧数是成对的**：同一段内容至少给两帧才看得出「有没有变化」——
    单帧只能说明「这一刻长这样」，说不出「然后呢」。所以基准给得比直觉宽：
    3 分钟内 4 帧、15 分钟内 6 帧、30 分钟内 8 帧，更长 10 帧。
    """

    if requested:
        return max(1, min(int(requested), 12))
    duration = max(int(duration or 0), 10)
    if duration <= 180:
        return 4
    if duration <= 900:
        return 6
    if duration <= 1800:
        return 8
    return 10


def smart_marks(duration: int, danmaku_items: list[dict], requested: int = 0) -> list[int]:
    """挑抽帧时间点：**一半跟着弹幕热度走，一半均匀铺开**。

    为什么不是均匀抽：视频的信息密度不是均匀的。有的段落全是干货（弹幕密集、
    观众在追着讨论），有的段落纯过渡（几十秒没人说话）。均匀抽很容易连着截到
    三段废话，而真正的高光一帧没截到。

    弹幕密度是个便宜的代理指标——它同时包含了「这里有事发生」和「观众有反应」。
    但全跟弹幕走也不行：吐槽点会挤在一起、覆盖不到全片。所以对半分。

    Args:
        duration: 视频总秒数。
        danmaku_items: 全量弹幕（``[{"t": 秒, "text": ...}]``）。
        requested: 强制帧数（0 = 按时长自动）。

    Returns:
        升序的时间点列表（秒）。
    """

    duration = max(int(duration or 0), 10)
    count = frame_count_for(duration, requested)
    items = [x for x in (danmaku_items or []) if x.get("t") is not None]

    marks: list[int] = []

    # ---- 一半：弹幕最密集的时段
    if items:
        bucket = max(8, duration // 40)  # 切成约 40 段
        buckets = max(1, duration // bucket + 1)
        density = [0] * buckets
        for item in items:
            try:
                at = float(item["t"])
            except (TypeError, ValueError):
                continue
            index = min(buckets - 1, max(0, int(at // bucket)))
            density[index] += 1

        # 掐掉首尾 10%：开头多是片头，结尾多是演职员表
        low = max(0, int(buckets * 0.10))
        high = max(low + 1, buckets - int(buckets * 0.10))
        ranked = sorted(
            ((density[i], i) for i in range(low, high)),
            reverse=True,
        )
        hot = max(1, count // 2)
        for _, index in ranked[:hot]:
            marks.append(min(duration - 2, index * bucket + bucket // 2))

    # ---- 另一半：均匀铺开，保证覆盖全片
    remaining = count - len(marks)
    for i in range(max(0, remaining)):
        frac = (i + 0.5) / max(1, remaining)
        marks.append(int(duration * (0.12 + 0.76 * frac)))
    if not marks:  # 没有弹幕也没关系，均匀兜底
        marks = [int(duration * (0.2 + 0.6 * i / max(1, count - 1))) for i in range(count)]

    # ---- 去重 & 排序：挨太近的合并，免得两次截到同一画面
    marks.sort()
    picked: list[int] = []
    for mark in marks:
        mark = max(1, min(duration - 2, int(mark)))
        if not picked or mark - picked[-1] >= max(6, duration // 60):
            picked.append(mark)
    return picked[:count] or [int(duration * 0.45)]


def sample_danmaku(items: list[dict], limit: int) -> list[dict]:
    """从全量弹幕里等间距抽几条给模型看（全量塞上下文纯属烧 token）。"""

    total = len(items)
    limit = max(1, int(limit or 30))
    if total <= limit:
        return list(items)
    step = max(1, total // limit)
    return items[::step][:limit]


def watch(
    client: BiliClient,
    link: str,
    *,
    frames: int = 3,
    profile_dir: str | Path,
    out_dir: str | Path,
    danmaku_limit: int = 30,
    page_timeout: float = 25.0,
) -> dict[str, Any]:
    """看一个视频：信息 + 弹幕 + 若干帧画面。"""

    bvid = extract_bvid(link)
    if not bvid:
        raise LinkError(f"认不出这是哪个视频：{link!r}（发 BV 号、视频链接、或 b23.tv 短链都行）")

    info = client.video(bvid)
    duration = int(info.get("时长秒") or 0)

    try:
        # 全量弹幕只用来算「哪里热闹」——不直接进上下文，太费 token
        full = client.danmaku(bvid, 3000)
        all_items = list(full.get("取样") or [])
        danmaku = {
            "标题": full.get("标题"),
            "弹幕总数": full.get("弹幕总数", len(all_items)),
            "取样": sample_danmaku(all_items, danmaku_limit),
        }
    except BiliError as exc:
        all_items = []
        danmaku = {"标题": info.get("标题"), "弹幕总数": 0, "取样": [], "错误": str(exc)}

    marks = smart_marks(duration, all_items, frames)
    shots: list[dict[str, Any]] = []
    browser_error = ""
    injected = 0
    browser = find_browser()

    if not browser:
        browser_error = "没找到 Chrome / Edge，只能看弹幕和简介"
    else:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        try:
            with HeadfulChrome(profile_dir, browser=browser, viewport=(1280, 720)) as chrome:
                # 这个 profile 是独立空目录（不能和 MCP 共用），先把登录态塞进去：
                # 不然 B 站只给试看 30 秒、还看不到高清
                injected = chrome.set_cookies(client.cookies)
                chrome.open(f"https://www.bilibili.com/video/{bvid}/", wait=2.5)
                if not chrome.wait_for("(() => !!document.querySelector('video'))()", timeout=page_timeout):
                    raise BrowserError("页面里没等到 video 元素（可能被风控拦了）")

                # 把播放器撑到整窗：画面从 ~800×450 变成整窗，细节多一倍。
                # 失败也不影响——下面量不到大尺寸就用原尺寸截。
                chrome.enlarge_video()
                time.sleep(0.9)

                # 静音：不吵主人；pause：我们要按帧定位，不需要连播
                chrome.eval_js(
                    "(() => { const v = document.querySelector('video');"
                    " if (!v) return false; v.muted = true; try { v.pause(); } catch (e) {}"
                    " return true; })()"
                )

                for index, mark in enumerate(marks, start=1):
                    try:
                        chrome.eval_js(
                            "(() => { const v = document.querySelector('video');"
                            f" if (!v) return -1; v.currentTime = {mark}; return v.currentTime; }})()"
                        )
                        ready = chrome.wait_for(
                            "(() => { const v = document.querySelector('video');"
                            f" return !!v && v.readyState >= 2 && Math.abs(v.currentTime - {mark}) < 2.5; }})()",
                            timeout=15.0,
                        )
                        time.sleep(0.8)  # 等这一帧真的绘出来，别截到上一帧
                        target = out / f"watch-{bvid}-{index:02d}.png"
                        # 只截视频区域：导航栏、推荐、评论统统裁掉，模型只需要画面
                        rect = chrome.video_rect()
                        if rect:
                            chrome.screenshot(target, clip=rect, scale=2.0)
                        else:
                            chrome.screenshot(target)
                        shots.append({"秒": mark, "图": str(target), "就绪": bool(ready)})
                    except BrowserError as exc:
                        shots.append({"秒": mark, "图": "", "错误": str(exc)})
        except BrowserError as exc:
            browser_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 浏览器这块什么怪事都可能
            browser_error = f"{type(exc).__name__}: {exc}"

    return {
        "bvid": bvid,
        "信息": info,
        "弹幕": danmaku,
        "抽帧": shots,
        "注入cookie": injected,
        "浏览器错误": browser_error,
    }


def render(result: dict[str, Any]) -> str:
    """把 :func:`watch` 的结果压成一段给人/给模型看的文本。"""

    info = result.get("信息") or {}
    lines: list[str] = []
    lines.append(
        f"《{info.get('标题')}》 UP：{info.get('up')}，"
        f"{info.get('时长秒')} 秒，播放 {info.get('播放')} / 点赞 {info.get('点赞')}。"
    )
    if info.get("简介"):
        lines.append(f"简介：{info['简介']}")

    danmaku = result.get("弹幕") or {}
    picked = danmaku.get("取样") or []
    if picked:
        lines.append(f"弹幕共 {danmaku.get('弹幕总数', 0)} 条，按时间取这些：")
        seen: set[str] = set()
        for item in picked:
            text = str(item.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            at = int(float(item.get("t") or 0))
            lines.append(f"  [{at // 60:02d}:{at % 60:02d}] {text}")
    else:
        lines.append("弹幕：一条都没抓到" + (f"（{danmaku.get('错误')}）" if danmaku.get("错误") else ""))

    shots = result.get("抽帧") or []
    if shots:
        ok_shots = [s for s in shots if s.get("图")]
        lines.append(
            f"画面：按时长和弹幕热度选了 {len(shots)} 个时间点，存了 {len(ok_shots)} 张"
            f"（只截视频区域并放大 2 倍）——"
        )
        for shot in shots:
            if shot.get("图"):
                at = int(shot.get("秒") or 0)
                lines.append(f"  [{at // 60:02d}:{at % 60:02d}] {shot['图']}")
            else:
                at = int(shot.get("秒") or 0)
                lines.append(f"  [{at // 60:02d}:{at % 60:02d}] 截失败：{shot.get('错误')}")
    else:
        lines.append("画面：一张都没截到")

    if result.get("浏览器错误"):
        lines.append(f"浏览器那块出问题：{result['浏览器错误']}（弹幕与信息不受影响）")
    elif result.get("注入cookie"):
        lines.append(f"（浏览器已注入 {result['注入cookie']} 条登录态 cookie）")

    description = result.get("画面描述")
    if description:
        lines.append("")
        lines.append("画面内容（视觉模型看的）：")
        lines.append(description.strip())
    elif result.get("描述错误"):
        lines.append(f"（画面描述失败：{result['描述错误']}）")

    return "\n".join(lines)


__all__ = ["watch", "render", "smart_marks", "sample_danmaku", "frame_count_for"]
