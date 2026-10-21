"""私信自动巡查：一个很小的自适应轮询状态机。

主人定的节奏
------------
- 平时 **10 分钟**查一次会话列表（``chat.idle_interval``）；
- 查到**对方发来**新私信 → 切 **10 秒**一次的高频（``chat.burst_interval``）；
- 高频连着 **6 次**都没查到新消息（对方没再回复）→ 退回 10 分钟一次
  （``chat.burst_checks``）。这期间又收到新消息会重新计数、继续高频。

为什么要高频这一段：私信是「一来一回」的对话，刚收到消息那一分钟对方很可能
还在接着打字；按 10 分钟一次去查就会一路错过。而一直 10 秒轮询又没必要——
所以只在「刚有动静」那一小段加密。

实现要点
--------
- **判定「新」用会话的最后消息时间戳**：``last_msg.timestamp``。最后一条是自己发的
  （``sender_uid == 我的 uid``）不算新消息——那是在等对方回，不是对方来信。
- **首次巡查只建基线、不报警**：否则刚开开关就把历史会话全当新私信刷一遍。
- 系统通知会话（``system_msg_type`` 非 0，talker_id 是 8.4e14 那种大数）全部跳过。
- 查到的内容只写日志 + 缓存文件，不主动往聊天流推；``bili_query action=chat``
  能读回最近缓存。
- 接口调用是同步阻塞的，统一丢进 ``asyncio.to_thread``，不堵事件循环。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from .client import BiliClient, BiliError

logger = get_logger("bili_whale.chat")

#: 单条缓存消息最多留多长（私信卡片能有一千多字，别整段塞缓存/上下文）
MAX_TEXT = 300


class ChatWatcher:
    """后台私信巡查器：调度器每拍调一次 :meth:`tick`。"""

    def __init__(
        self,
        client: BiliClient,
        *,
        idle_interval: int = 600,
        burst_interval: int = 10,
        burst_checks: int = 6,
        feed_path: str | Path | None = None,
        feed_keep: int = 60,
        max_sessions: int = 20,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self.idle = max(30, int(idle_interval))
        self.burst = max(3, int(burst_interval))
        self.checks = max(1, int(burst_checks))
        self._feed_path = Path(feed_path).expanduser() if feed_path else None
        self._keep = max(10, int(feed_keep))
        self._max_sessions = max(1, int(max_sessions))
        # 可注入时钟：自检里用它把「10 分钟 / 10 秒」压成秒级，生产里就是 monotonic
        self._clock: Callable[[], float] = clock or time.monotonic

        self._mode = "idle"
        self._burst_left = 0
        self._next_at = 0.0
        self._primed = False
        self._baseline: dict[int, int] = {}
        self._checks_done = 0
        self._errors = 0
        self._feed: list[dict[str, Any]] = self._load_feed()

    # ------------------------------------------------------------------ 对外

    @property
    def mode(self) -> str:
        """``idle`` / ``burst``。"""

        return self._mode

    def state(self) -> dict[str, Any]:
        """给查询工具和日志用的状态快照。"""

        now = self._clock()
        return {
            "模式": "高频" if self._mode == "burst" else "空闲",
            "间隔秒": self.burst if self._mode == "burst" else self.idle,
            "下次巡查秒": max(0, int(self._next_at - now)),
            "剩余高频次数": max(0, self._burst_left) if self._mode == "burst" else 0,
            "已建基线": self._primed,
            "会话数": len(self._baseline),
            "累计巡查": self._checks_done,
            "失败次数": self._errors,
            "缓存条数": len(self._feed),
        }

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """最近缓存到的新私信（新的在后）。"""

        return self._feed[-max(1, int(limit)):]

    async def tick(self) -> None:
        """调度器回调：该查就查，查完按状态机决定下一拍。"""

        now = self._clock()
        if now < self._next_at:
            return

        # 先按**当前**模式排下一拍：就算这次请求抛异常，也不会变成疯狂重试。
        # 真切换了模式的话，处理完会再用 _reschedule 按新模式重排。
        self._next_at = now + (self.burst if self._mode == "burst" else self.idle)

        first_run = not self._primed
        try:
            events = await asyncio.to_thread(self._poll, first_run)
        except BiliError as exc:
            self._errors += 1
            logger.warning(f"[chat] 巡查失败（登录态/网络）：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 巡查异常不打断调度
            self._errors += 1
            logger.warning(f"[chat] 巡查异常（{type(exc).__name__}）：{exc}")
            return

        self._checks_done += 1
        if first_run:
            self._primed = True
            self._reschedule(now)
            logger.info(
                f"[chat] 首次巡查建立基线：{len(self._baseline)} 个会话；"
                f"平时每 {self.idle}s 查，收到新私信转 {self.burst}s"
            )
            return

        if events:
            self._record(events)
            self._mode = "burst"
            self._burst_left = self.checks
            # 关键：转高频后必须把下一拍改成 burst 间隔，否则要等满 idle 才再查
            self._reschedule(now)
            names = "、".join(str(item.get("对方") or "?") for item in events[:5])
            logger.info(
                f"[chat] 收到 {len(events)} 条新私信（{names}）→ "
                f"转 {self.burst}s 高频，最多 {self.checks} 次"
            )
            return

        if self._mode != "burst":
            # 空闲且没新消息：_next_at 已按 idle 排好，不用再动
            return

        self._burst_left -= 1
        if self._burst_left <= 0:
            self._mode = "idle"
            logger.info(f"[chat] 高频 {self.checks} 次没等到新消息 → 退回每 {self.idle}s 巡查")
        self._reschedule(now)

    def _reschedule(self, now: float) -> None:
        """按**当前**模式重排下一拍。"""

        self._next_at = now + (self.burst if self._mode == "burst" else self.idle)

    # ------------------------------------------------------------------ 内部

    def _poll(self, first_run: bool) -> list[dict[str, Any]]:
        """查一轮会话列表，返回这一轮发现的新私信（同步，跑在线程池里）。"""

        client = self._client
        payload = client.chat_sessions(size=self._max_sessions)
        sessions = payload.get("会话") or []

        # 会话列表不返回普通用户的昵称，批量补一次（一次请求，别一个个查）
        need_names = [
            int(item["talker_id"])
            for item in sessions
            if not item.get("系统") and not item.get("昵称") and int(item.get("talker_id") or 0) > 0
        ]
        names: dict[int, str] = {}
        if need_names:
            try:
                names = client.chat_peer_names(need_names)
            except BiliError:
                names = {}

        events: list[dict[str, Any]] = []
        for item in sessions:
            talker = int(item.get("talker_id") or 0)
            if item.get("系统") or talker <= 0:
                continue

            stamp = int(item.get("last_ts") or 0)
            previous = self._baseline.get(talker)

            if item.get("我发的"):
                # 最后一条是我发的：在等对方回，只推进基线，不算新私信
                self._baseline[talker] = max(previous or 0, stamp)
                continue

            is_new = (not first_run) and (previous is None or stamp > previous)
            self._baseline[talker] = max(previous or 0, stamp)
            if not is_new:
                continue

            # 新会话（第一次来信）也走这里：previous 为 None
            texts: list[str] = []
            try:
                thread = client.chat_messages(talker, size=6)
                for msg in thread.get("消息") or []:
                    if msg.get("我"):
                        continue
                    text = str(msg.get("文本") or "").strip()
                    if text:
                        texts.append(text)
            except BiliError:
                texts = []

            body = " / ".join(texts[-3:]) if texts else str(item.get("摘要") or "")
            events.append({
                "时间戳": stamp,
                "时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "",
                "对方": names.get(talker) or str(item.get("昵称") or "") or f"uid{talker}",
                "talker_id": talker,
                "内容": body[:MAX_TEXT],
            })

        events.sort(key=lambda row: int(row.get("时间戳") or 0))
        return events

    def _record(self, events: list[dict[str, Any]]) -> None:
        self._feed.extend(events)
        if len(self._feed) > self._keep:
            self._feed = self._feed[-self._keep:]
        self._save_feed()

    def _load_feed(self) -> list[dict[str, Any]]:
        path = getattr(self, "_feed_path", None)
        if path is None or not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("消息") if isinstance(data, dict) else data
            keep = getattr(self, "_keep", 60)
            return [item for item in (items or []) if isinstance(item, dict)][-keep:]
        except Exception as exc:  # noqa: BLE001 - 缓存坏了就当空的，别拦启动
            logger.warning(f"[chat] 读缓存失败（{type(exc).__name__}），先当空的用：{exc}")
            return []

    def _save_feed(self) -> None:
        path = self._feed_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "更新": time.strftime("%Y-%m-%d %H:%M:%S"),
                "条数": len(self._feed),
                "消息": self._feed,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响巡查
            logger.warning(f"[chat] 写缓存失败（{type(exc).__name__}）：{exc}")


__all__ = ["ChatWatcher"]
