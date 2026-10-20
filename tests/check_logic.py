# -*- coding: utf-8 -*-
r"""端到端逻辑自检：真的装一遍插件、真的调一次 B 站。

和 ``check_import.py`` 的区别：那个只证明「能导入」，这个证明「装上去真的干活」——
走完整加载路径（``on_plugin_loaded`` → 读登录态 → 建客户端 → 注册组件），
再拿真工具去查一次账号、订单、视频、弹幕。

用法（实例自带 venv）：

    $env:NEO_ROOT="F:\Neo-MoFox-Aemeath"
    & "F:\Neo-MoFox-Aemeath\.venv\Scripts\python.exe" "F:\mofox插件\plugin\repo\bili_whale\tests\check_logic.py"

只跑**只读**查询。写操作在这里只验证「默认被拦住」，绝不真发东西。
"""

from __future__ import annotations

import asyncio
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

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# 真实 bot 启动时会调 init_model_config（bot.py:319）；这里自检也得补上，
# 否则视觉模型那一步会以「Model config not initialized」降级。
try:
    from src.core.config.model_config import init_model_config

    init_model_config(str(NEO_ROOT / "config" / "model.toml"))
    print("· 模型配置已初始化（视觉描述可用）")
except Exception as exc:  # noqa: BLE001 - 失败就按降级路径测
    print(f"· 模型配置初始化失败（视觉描述会降级）：{type(exc).__name__}: {exc}")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


from bili_whale import runtime  # noqa: E402
from bili_whale.config import BiliWhaleConfig  # noqa: E402
from bili_whale.plugin import BiliWhalePlugin  # noqa: E402
from bili_whale.tools import BiliActionTool, BiliCatalogTool, BiliQueryTool  # noqa: E402


def build_plugin(**overrides) -> BiliWhalePlugin:
    """造一个装了配置的插件实例（不经过框架加载器，但走同一套构造路径）。"""

    config = BiliWhaleConfig()
    for key, value in overrides.items():
        section, field = key.split("__", 1)
        setattr(getattr(config, section), field, value)
    plugin = BiliWhalePlugin(config)
    # 组件通常在注册后被填入，这里手动对齐（工具只用到 plugin.config）
    return plugin


async def main() -> None:
    print(f"框架根：{NEO_ROOT}")
    print(f"插件目录：{PLUGIN_DIR}")

    # ---------------------------------------------------------- 1. 加载路径
    plugin = build_plugin()
    try:
        await plugin.on_plugin_loaded()
        check("on_plugin_loaded 正常返回", True)
    except Exception as exc:  # noqa: BLE001
        check("on_plugin_loaded", False, f"{type(exc).__name__}: {exc}")

    client = runtime.get_client()
    check("登录态装载成功", client is not None, runtime.login_error() or f"uid={getattr(client, 'uid', '?')}")
    if client is None:
        print("\n登录态没装上，后面的真实查询跳过。")
        return

    # ---------------------------------------------------------- 2. 组件
    components = plugin.get_components()
    check("注册四个工具", len(components) == 4, str([c.name for c in components]))

    catalog = BiliCatalogTool(plugin)
    query = BiliQueryTool(plugin)
    action = BiliActionTool(plugin)

    # ---------------------------------------------------------- 3. 真实只读
    ok, text = await catalog.execute()
    check("目录可用", ok and "订单" in text and "弹幕" in text, f"{len(text)} 字")

    try:
        ok, text = await query.execute("account")
        check("查账号", ok and "Lv" in text, text.split("\n")[0][:50])
    except Exception as exc:  # noqa: BLE001
        check("查账号", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("unread")
        check("查未读", ok, text.split("\n")[0][:50])
    except Exception as exc:  # noqa: BLE001
        check("查未读", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("orders", "todo")
        check("查待办订单", ok and "待办" in text, text.split("\n")[0][:60])
    except Exception as exc:  # noqa: BLE001
        check("查待办订单", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("order", "8001220220438018")
        check("查订单详情", ok and "订单" in text, text.split("\n")[0][:60])
    except Exception as exc:  # noqa: BLE001
        check("查订单详情", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("video", "BV11SJt63ERg")
        check("查视频", ok and "秒" in text, text.split("\n")[0][:60])
    except Exception as exc:  # noqa: BLE001
        check("查视频", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("danmaku", "BV11SJt63ERg", 12)
        check("抓弹幕", ok and "条弹幕" in text, text.split("\n")[0][:60])
    except Exception as exc:  # noqa: BLE001
        check("抓弹幕", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("history", "", 5)
        check("查历史", ok, text.split("\n")[0][:50])
    except Exception as exc:  # noqa: BLE001
        check("查历史", False, f"{type(exc).__name__}: {exc}")

    try:
        ok, text = await query.execute("favs")
        check("查收藏夹", ok and "收藏夹" in text, text.split("\n")[0][:60])
    except Exception as exc:  # noqa: BLE001
        check("查收藏夹", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 4. 写闸门
    # 默认是开的（主人的号），但空内容必须在发出去之前就被拦下
    try:
        ok, text = await action.execute("reply", "BV11SJt63ERg", "")
        check("空评论被拦（不会真空发）", (not ok) and "不能为空" in text, text[:40])
    except Exception as exc:  # noqa: BLE001
        check("写操作闸门", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 4.5 链接解析
    from bili_whale import links

    cases = [
        ("BV11SJt63ERg", "BV11SJt63ERg"),
        ("https://www.bilibili.com/video/BV11SJt63ERg/?spm_id_from=333.1007", "BV11SJt63ERg"),
        ("看看这个 av170001 挺有意思", "av170001"),
    ]
    for raw, expect in cases:
        try:
            got = links.extract_bvid(raw)
            check(f"解析 {raw[:34]}", got == expect, f"{got}（期望 {expect}）")
        except Exception as exc:  # noqa: BLE001
            check(f"解析 {raw[:34]}", False, f"{type(exc).__name__}: {exc}")

    check("认得出这是 B 站链接", links.looks_like_bili_link("https://b23.tv/AbCdEf1"))
    check("普通文本不会被误判成链接", not links.looks_like_bili_link("今天天气不错"))

    # ---------------------------------------------------------- 4.6 看视频
    try:
        from bili_whale.tools import BiliWatchTool

        watch_tool = BiliWatchTool(plugin)
        ok, text = await watch_tool.execute("https://www.bilibili.com/video/BV11SJt63ERg/?spm_id_from=333.1007")
        got_frames = "watch-BV11SJt63ERg" in text
        check("看视频（真起浏览器抽帧）", ok and got_frames, text.split("\n")[-2][:80] if text else "空")
        # 视觉模型描述这一层（模型配置起不来时会降级，不算失败）
        if "画面内容" in text:
            desc = text.split("画面内容")[-1].strip()[:120].replace("\n", " ")
            print(f"· 视觉模型描述：{desc}")
        else:
            print("· 视觉模型描述缺失（已降级为只给截图路径，不影响主结果）")
    except Exception as exc:  # noqa: BLE001
        check("看视频", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 5. 卸载
    try:
        await plugin.on_plugin_unloaded()
        check("卸载后客户端已清空", runtime.get_client() is None)
    except Exception as exc:  # noqa: BLE001
        check("on_plugin_unloaded", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 6. 缺登录态降级
    broken = build_plugin(plugin__cookie_file="data/__not_exist__.json")
    try:
        await broken.on_plugin_loaded()
        ok, text = await BiliQueryTool(broken).execute("account")
        check("缺登录态时明确报错而不是崩", (not ok) and "登录态" in text, text[:50].replace("\n", " "))
    except Exception as exc:  # noqa: BLE001
        check("缺登录态降级", False, f"{type(exc).__name__}: {exc}")


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
