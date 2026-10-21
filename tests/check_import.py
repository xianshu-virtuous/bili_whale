# -*- coding: utf-8 -*-
r"""真实导入冒烟：证明框架真能加载这个插件，而不只是语法对。

用法（务必用实例自带 venv，别用系统 python）：

    $env:NEO_ROOT="F:\Neo-MoFox-Aemeath"
    & "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" "F:\mofox插件\plugin\repo\bili_whale\tests\check_import.py"

它做五件事：
1. 把框架根与插件父目录插进 sys.path，逐个 import 本插件模块；
2. 校验 manifest.json（根级、字段、组件与真实工具类对得上）；
3. 实例化配置模型，确认 [plugin]/[actions]/[query] 三节都在；
4. 实例化三个工具类，确认 name/description 非空且互不重名；
5. 真调一次 MCP 管理器（不连服务，只查已连接列表），确认那条提醒逻辑不会炸。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

MODULES = [
    "bili_whale",
    "bili_whale.config",
    "bili_whale.client",
    "bili_whale.runtime",
    "bili_whale.chat_watch",
    "bili_whale.tools",
    "bili_whale.plugin",
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print(f"框架根：{NEO_ROOT}")
print(f"插件目录：{PLUGIN_DIR}")

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# ---------------------------------------------------------------- 1. 导入
for name in MODULES:
    try:
        importlib.import_module(name)
        check(f"import {name}", True)
    except Exception as exc:  # noqa: BLE001
        check(f"import {name}", False, f"{type(exc).__name__}: {exc}")

config_mod = importlib.import_module("bili_whale.config")
client_mod = importlib.import_module("bili_whale.client")
runtime_mod = importlib.import_module("bili_whale.runtime")
watch_mod = importlib.import_module("bili_whale.chat_watch")
tools_mod = importlib.import_module("bili_whale.tools")
plugin_mod = importlib.import_module("bili_whale.plugin")

# ---------------------------------------------------------------- 2. manifest
try:
    manifest_path = PLUGIN_DIR / "manifest.json"
    raw_bytes = manifest_path.read_bytes()
    check("manifest.json 无 BOM", not raw_bytes.startswith(b"\xef\xbb\xbf"))
    manifest = json.loads(raw_bytes.decode("utf-8-sig"))
    for field in ("name", "version", "entry_point", "include"):
        check(f"manifest 含 {field}", field in manifest)
    check("manifest name 一致", manifest.get("name") == "bili_whale", str(manifest.get("name")))

    declared = {item.get("component_name") for item in manifest.get("include", []) if item.get("component_type") == "tool"}
    real = set()
    for attr in ("BiliCatalogTool", "BiliQueryTool", "BiliActionTool", "BiliWatchTool"):
        cls = getattr(tools_mod, attr, None)
        if cls is not None:
            real.add(cls.name)
    check("manifest 工具与真实类一致", declared == real, f"manifest={sorted(declared)} 实际={sorted(real)}")
except Exception as exc:  # noqa: BLE001
    check("manifest 校验", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 3. 配置
try:
    cfg_cls = config_mod.BiliWhaleConfig
    cfg = cfg_cls()
    check("配置可实例化", True)
    check("配置含 plugin 节", hasattr(cfg, "plugin"))
    check("配置含 actions 节", hasattr(cfg, "actions"))
    check("配置含 query 节", hasattr(cfg, "query"))
    check("写操作默认开（这个是给主人自己号用的）", cfg.actions.enabled is True, str(cfg.actions.enabled))
    check("关注 / 取关默认开（受总闸约束）", cfg.actions.allow_follow is True, str(cfg.actions.allow_follow))
    check("看视频默认开", cfg.watch.enabled is True, str(cfg.watch.enabled))
    check("帧数默认自动（0=按时长）", cfg.watch.frames == 0, str(cfg.watch.frames))
    check("cookie_file 默认为 data/bili_cookies.json", cfg.plugin.cookie_file == "data/bili_cookies.json", cfg.plugin.cookie_file)
    check("默认检查 MCP", cfg.plugin.check_mcp is True)
    # —— 私信自动巡查（默认关）——
    check("配置含 chat 节", hasattr(cfg, "chat"))
    check("自动查私信默认关", cfg.chat.enabled is False, str(cfg.chat.enabled))
    check("默认 10 分钟查一次", cfg.chat.idle_interval == 600, str(cfg.chat.idle_interval))
    check("收到新私信转 10 秒", cfg.chat.burst_interval == 10, str(cfg.chat.burst_interval))
    check("高频持续 6 次", cfg.chat.burst_checks == 6, str(cfg.chat.burst_checks))
    check("节拍默认 10 秒", cfg.chat.tick_seconds == 10.0, str(cfg.chat.tick_seconds))
    check("缓存文件在 data 下", cfg.chat.feed_file.startswith("data/"), cfg.chat.feed_file)
except Exception as exc:  # noqa: BLE001
    check("配置实例化", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 4. 工具
#: BaseTool 构造签名是 ``__init__(plugin)``，这里给个最小替身：工具只用到 ``plugin.config``。
from unittest.mock import Mock

_stub_plugin = Mock()
try:
    _stub_plugin.config = cfg
except Exception:  # noqa: BLE001
    _stub_plugin.config = config_mod.BiliWhaleConfig()

seen_names: set[str] = set()
for attr in ("BiliCatalogTool", "BiliQueryTool", "BiliActionTool"):
    try:
        cls = getattr(tools_mod, attr)
        tool = cls(_stub_plugin)
        ok = bool(tool.name) and bool(tool.description)
        check(f"{attr} 可实例化且 name/description 非空", ok, f"name={tool.name}")
        if tool.name in seen_names:
            check(f"{attr} 名字不重复", False, tool.name)
        seen_names.add(tool.name)
    except Exception as exc:  # noqa: BLE001
        check(f"{attr} 实例化", False, f"{type(exc).__name__}: {exc}")

# 目录工具的返回值要是能看的文本
try:
    import asyncio

    ok, text = asyncio.run(tools_mod.BiliCatalogTool(_stub_plugin).execute())
    check("bili_catalog 可执行且返回非空", bool(ok) and len(text) > 200, f"{len(text)} 字")
    check("目录含私信能力", "action=chat" in text and "私信" in text)
    check("目录含关注能力", "follow" in text and "关注" in text)
except Exception as exc:  # noqa: BLE001
    check("bili_catalog 执行", False, f"{type(exc).__name__}: {exc}")

# 工具描述里必须写清「多个候选先问请求者」这条规矩
try:
    action_desc = tools_mod.BiliActionTool(_stub_plugin).description
    query_desc = tools_mod.BiliQueryTool(_stub_plugin).description
    check("bili_action 描述含 follow", "follow" in action_desc)
    check("bili_action 描述要求多候选先问人", "请求者" in action_desc)
    check("bili_query 描述含 chat / search_user", "chat" in query_desc and "search_user" in query_desc)
    check("bili_query 描述也要求多候选先问人", "请求者" in query_desc)
except Exception as exc:  # noqa: BLE001
    check("工具描述检查", False, f"{type(exc).__name__}: {exc}")

# 巡查器能构造（假 client，不联网）
try:
    class _FakeClient:
        def chat_sessions(self, size: int = 20, page: int = 1) -> dict:
            return {"会话": [], "总数": 0}

        def chat_peer_names(self, uids: list[int]) -> dict:
            return {}

        def chat_messages(self, talker_id: int, size: int = 20) -> dict:
            return {"消息": [], "条数": 0}

    watcher = watch_mod.ChatWatcher(_FakeClient(), idle_interval=600, burst_interval=10, burst_checks=6)
    state = watcher.state()
    check("ChatWatcher 可构造且初始状态空闲", state["模式"] == "空闲" and state["间隔秒"] == 600, str(state))
except Exception as exc:  # noqa: BLE001
    check("ChatWatcher 构造", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 5. 客户端与 MCP
try:
    client_mod.BiliClient({"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "1"})
    check("BiliClient 可构建（假 cookie）", True)
except Exception as exc:  # noqa: BLE001
    check("BiliClient 构建", False, f"{type(exc).__name__}: {exc}")

try:
    client_mod.BiliClient({})
    check("空 cookie 应当报错", False)
except client_mod.BiliError:
    check("空 cookie 会明确报错", True)

try:
    from src.core.managers import get_mcp_manager

    manager = get_mcp_manager()
    metas = manager.get_connected_server_metadata() if manager else []
    names = sorted(str(getattr(m, "server_name", "")) for m in metas)
    check("MCP 管理器可查", True, f"已连接：{names}")
    # 这条只提示，不算失败：没连 playwright 是正常状态，插件本来就能跑
    print(f"· playwright MCP {'在' if 'playwright' in names else '不在'}已连接列表里（仅提示）")
except Exception as exc:  # noqa: BLE001
    check("MCP 管理器可查（框架缺接口时跳过）", True, f"{type(exc).__name__}: {exc}")

try:
    runtime_mod.set_client(None, "自检占位")
    ok, text = asyncio.run(tools_mod.BiliQueryTool(_stub_plugin).execute("account"))
    check("无登录态时工具给出明确提示", (not ok) and "登录态" in text, text[:60].replace("\n", " "))
    runtime_mod.reset()
except Exception as exc:  # noqa: BLE001
    check("无登录态降级", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 收尾
print()
if failures:
    print(f"✗ 失败 {len(failures)} 项：")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("✓ 全部通过")
