# -*- coding: utf-8 -*-
r"""私信巡查状态机自检（**纯离线**，不碰网络、不碰真实账号）。

验证主人定的那套节奏：

- 首次巡查只建基线，不把历史会话当新私信；
- 平时 ``idle_interval`` 秒查一次；
- 查到对方新私信 → 转 ``burst_interval`` 秒，连续 ``burst_checks`` 次没有新消息就退回低频；
- 高频期间又收到新消息 → 重新计数，继续高频；
- 「最后一条是我发的」不算新私信（那是在等对方回）；
- 系统通知会话跳过；新会话（第一次来信）算新私信；
- 缓存文件能落盘、再读回来。

用法（实例自带 venv）：

    & "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" "F:\mofox插件\plugin\repo\bili_whale\tests\check_watch.py"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent
sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

from bili_whale.chat_watch import ChatWatcher  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class FakeClient:
    """只实现巡查用到的三个方法；会话快照由测试随时改。"""

    def __init__(self) -> None:
        self.sessions: list[dict] = []
        self.names: dict[int, str] = {}
        self.threads: dict[int, list[dict]] = {}
        self.session_calls = 0
        self.message_calls = 0

    # 测试辅助
    def post(self, talker: int, stamp: int, *, mine: bool = False, text: str = "在吗") -> None:
        self.sessions = [s for s in self.sessions if s["talker_id"] != talker]
        self.sessions.insert(0, {
            "talker_id": talker,
            "系统": False,
            "unread": 0 if mine else 1,
            "last_ts": stamp,
            "last_sender_uid": 1 if mine else talker,
            "我发的": mine,
            "昵称": "",
            "消息类型": 1,
            "摘要": text,
        })
        if not mine:
            self.threads.setdefault(talker, []).append({
                "时间": "10-21 08:00",
                "时间戳": stamp,
                "我": False,
                "类型": 1,
                "文本": text,
            })

    def system_session(self, talker: int = 844424930131966, stamp: int = 500) -> None:
        self.sessions.append({
            "talker_id": talker,
            "系统": True,
            "unread": 0,
            "last_ts": stamp,
            "last_sender_uid": talker,
            "我发的": False,
            "昵称": "哔哩哔哩智能姬",
            "消息类型": 10,
            "摘要": "你关注的 UP 更新了",
        })

    # 被测接口
    def chat_sessions(self, size: int = 20, page: int = 1) -> dict:
        self.session_calls += 1
        return {"会话": [dict(item) for item in self.sessions], "总数": len(self.sessions)}

    def chat_peer_names(self, uids: list[int]) -> dict[int, str]:
        return {int(uid): self.names.get(int(uid), "") for uid in uids}

    def chat_messages(self, talker_id: int, size: int = 20) -> dict:
        self.message_calls += 1
        rows = self.threads.get(int(talker_id), [])
        return {"消息": rows[-max(1, size):], "条数": len(rows)}


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="bili_watch_"))
    feed_path = workdir / "chat_feed.json"

    now = [1000.0]
    fake = FakeClient()
    fake.names[111] = "小明明"
    watcher = ChatWatcher(
        fake,
        idle_interval=600,
        burst_interval=10,
        burst_checks=6,
        feed_path=feed_path,
        clock=lambda: now[0],
    )

    # ---------------------------------------------------------------- 首次基线
    fake.post(111, 100)
    fake.system_session()
    await watcher.tick()
    check("首拍建立基线", watcher.state()["已建基线"] is True)
    check("首拍不把历史会话当新私信", watcher.recent() == [])
    check("首拍只请求一次", fake.session_calls == 1, str(fake.session_calls))
    check("系统通知会话不进基线", watcher.state()["会话数"] == 1, str(watcher.state()["会话数"]))

    # ---------------------------------------------------------------- 还没到点
    now[0] += 100
    await watcher.tick()
    check("不到 idle 间隔不请求", fake.session_calls == 1, str(fake.session_calls))

    # ---------------------------------------------------------------- 到点没新消息
    now[0] = 1000 + 600
    await watcher.tick()
    check("到点查一次", fake.session_calls == 2, str(fake.session_calls))
    check("没新消息保持空闲", watcher.mode == "idle", watcher.mode)

    # ---------------------------------------------------------------- 新私信 → 高频
    fake.post(111, 700)
    now[0] += 600
    await watcher.tick()
    check("收到新私信转高频", watcher.mode == "burst", watcher.mode)
    state = watcher.state()
    check("高频剩余次数 = 6", state["剩余高频次数"] == 6, str(state["剩余高频次数"]))
    cached = watcher.recent()
    check("新私信进缓存", len(cached) == 1, str(len(cached)))
    check("缓存里有对方名字", cached and cached[0].get("对方") == "小明明", str(cached[0].get("对方") if cached else ""))
    check("缓存里有正文", cached and "在吗" in str(cached[0].get("内容")), str(cached[0].get("内容") if cached else ""))
    check("缓存文件已落盘", feed_path.is_file())

    # ---------------------------------------------------------------- 高频 6 次没新消息 → 回退
    before = fake.session_calls
    for _ in range(5):
        now[0] += 10
        await watcher.tick()
    check("高频期间保持 burst", watcher.mode == "burst", watcher.mode)
    check("高频剩余 1 次", watcher.state()["剩余高频次数"] == 1, str(watcher.state()["剩余高频次数"]))
    now[0] += 10
    await watcher.tick()
    check("第 6 次没消息退回低频", watcher.mode == "idle", watcher.mode)
    check("高频确实按 10 秒查了 6 次", fake.session_calls - before == 6, str(fake.session_calls - before))

    # ---------------------------------------------------------------- 高频期间又来消息 → 重新计数
    fake.post(111, 900, text="在吗在吗")
    now[0] += 600
    await watcher.tick()
    check("再次收到新私信转高频", watcher.mode == "burst", watcher.mode)
    for _ in range(3):
        now[0] += 10
        await watcher.tick()
    check("3 次后剩 3 次", watcher.state()["剩余高频次数"] == 3, str(watcher.state()["剩余高频次数"]))
    fake.post(111, 950, text="人呢")
    now[0] += 10
    await watcher.tick()
    check("高频期间又来消息重置计数", watcher.state()["剩余高频次数"] == 6, str(watcher.state()["剩余高频次数"]))
    check("两条内容都进缓存", len(watcher.recent()) == 3, str(len(watcher.recent())))

    # ---------------------------------------------------------------- 我发的不算新私信
    for _ in range(6):
        now[0] += 10
        await watcher.tick()
    check("高频窗口 6 次后回到空闲", watcher.mode == "idle", watcher.mode)

    fake.post(222, 1200, mine=True)
    now[0] += 600
    await watcher.tick()
    check("「最后一条是我发的」不触发高频", watcher.mode == "idle", watcher.mode)
    check("基线记住了新会话", watcher.state()["会话数"] == 2, str(watcher.state()["会话数"]))

    # ---------------------------------------------------------------- 新会话第一次来信算新私信
    fake.post(333, 1300, text="你好，我是新来的")
    now[0] += 600
    await watcher.tick()
    check("新会话第一次来信算新私信", watcher.mode == "burst", watcher.mode)
    check("新会话进缓存", any(item.get("talker_id") == 333 for item in watcher.recent()))

    # ---------------------------------------------------------------- 缓存文件能读回
    reloaded = ChatWatcher(fake, feed_path=feed_path, clock=lambda: now[0])
    check("缓存文件可读回", len(reloaded.recent()) == len(watcher.recent()), f"{len(reloaded.recent())} vs {len(watcher.recent())}")
    raw = json.loads(feed_path.read_text(encoding="utf-8"))
    check("缓存文件是带条数的 JSON", isinstance(raw, dict) and "条数" in raw)

    # ---------------------------------------------------------------- 缓存上限
    capped_now = [5000.0]
    capped_fake = FakeClient()
    capped = ChatWatcher(
        capped_fake,
        idle_interval=30,
        burst_interval=3,
        burst_checks=6,
        feed_path=workdir / "capped.json",
        feed_keep=10,
        clock=lambda: capped_now[0],
    )
    await capped.tick()  # 首拍建基线
    for index in range(12):
        capped_fake.post(999, 6000 + index, text=f"刷屏 {index}")
        capped_now[0] += 30
        await capped.tick()
    check("缓存条数有上限", len(capped.recent(999)) <= 10, str(len(capped.recent(999))))

    print(f"\n临时目录：{workdir}")


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("被中断")

print()
if failures:
    print(f"✗ 失败 {len(failures)} 项：")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("✓ 全部通过")
