"""bili_whale 插件入口：让深深像主人一样用 B 站。

装上之后会发生什么
------------------
- 给她三个工具：
  - ``bili_catalog`` —— 能力目录（每件事该调哪个工具、哪个 action）；
  - ``bili_query`` —— 只读：账号 / 未读 / 视频 / 弹幕 / 订单 / 历史 / 收藏夹 / 关注；
  - ``bili_action`` —— 写：评论 / 弹幕 / 点赞 / 收藏（默认开，装别人的实例建议关掉）。
- 登录态只读一份 ``cookies.json``（Playwright 一次性导出），之后所有请求都是纯 HTTP
  + wbi 签名——不常驻浏览器，也不吃 MCP 的调用开销。

**为什么依赖 MCP 却又不是必须的**：登录态过期那天，唯一省事的重新导出方式就是
playwright MCP（打开 B 站 → ``Network.getAllCookies`` → 写成 json）。所以加载时顺手
查一眼 MCP 管理器里有没有这个服务：没连上只打一条 warning，其余功能照跑——真的
过期了，工具会明确告诉她「需要主人重新导出一份」，而不是悄悄失败。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from . import runtime
from .chat_watch import ChatWatcher
from .client import BiliClient, BiliError, load_cookies
from .config import BiliWhaleConfig
from .tools import BiliActionTool, BiliCatalogTool, BiliQueryTool, BiliWatchTool

logger = get_logger("bili_whale")

_PLUGIN_DIR = Path(__file__).resolve().parent

#: 调度器里的任务名（固定名字，重载时靠它覆盖/移除）
_WATCH_TASK_NAME = "bili_whale_chat_watch"


@register_plugin
class BiliWhalePlugin(BasePlugin):
    """B站账号管家：查账号、看视频弹幕、翻会员购订单、替主人发评论弹幕。"""

    plugin_name: str = "bili_whale"
    plugin_description: str = (
        "让深深像主人一样用 B 站：账号概况、未读消息、观看历史、收藏夹、关注列表，"
        "视频信息与弹幕（看懂视频最省钱的方式），会员购订单列表与详情（含待收货、快递单号、"
        "定金尾款截止），以及发评论、发弹幕、点赞、收藏这些动手的活。"
        "全部走纯 HTTP + wbi 签名，登录态是一份从 Playwright 导出的 cookies.json——"
        "所以依赖 MCP（没连上只提醒，不影响使用）；写操作默认开——这个插件就是给主人自己的号用的，"
        "装到别人的实例上把 [actions] enabled 关掉即可。"
    )
    plugin_version: str = "1.1.0"
    configs: list[type] = [BiliWhaleConfig]

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._watch_task_id: str | None = None
        self._watch_schedule_id: str | None = None

    def get_components(self) -> list[type]:
        """配置里关掉就整体下线（一个工具都不注册）。"""

        config = self.config
        if isinstance(config, BiliWhaleConfig) and not config.plugin.enabled:
            return []
        return [BiliCatalogTool, BiliQueryTool, BiliActionTool, BiliWatchTool]

    async def on_plugin_loaded(self) -> None:
        """装载登录态、按配置起私信巡查、提示 MCP 与写操作开关状态。"""

        config = self.config
        if not isinstance(config, BiliWhaleConfig):
            logger.error("bili_whale: 配置加载失败，插件不生效")
            return

        self._load_login(config)
        self._check_mcp(config)
        self._start_chat_watch(config)

        if not config.actions.enabled:
            logger.info(
                "bili_whale: 写操作总闸关着（[actions] enabled = false）——"
                "只能查，不能发评论/弹幕。要动手请主人自己改配置。"
            )
        else:
            allowed = []
            if config.actions.allow_reply:
                allowed.append("评论")
            if config.actions.allow_danmaku:
                allowed.append("弹幕")
            if config.actions.allow_like:
                allowed.append("点赞")
            if config.actions.allow_fav:
                allowed.append("收藏")
            if config.actions.allow_follow:
                allowed.append("关注")
            logger.info(f"bili_whale: 写操作已开，允许：{'、'.join(allowed) or '（无）'}")

    async def on_plugin_unloaded(self) -> None:
        """卸掉后台巡查任务、清掉客户端单例（cookie 文件不动）。"""

        await self._stop_chat_watch()
        runtime.reset()
        logger.info("bili_whale: 已卸载")

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _framework_root() -> Path:
        """定位框架根目录。

        三种来源，按可靠性排：

        1. 环境变量 ``NEO_ROOT``（自检脚本、外部调用最稳）；
        2. 从本文件往上找带 ``main.py`` 的那一级（插件装进 ``<根>\\plugins\\<插件>`` 时命中）；
        3. 兜底：往上两级（开发目录 ``repo\\<插件>`` 的常见布局）。

        早期版本写死「往上三级」，真机一跑就找到 ``F:\\mofox插件\\data`` 去了——
        这是实测踩出来的，别再改回去。
        """

        env = os.environ.get("NEO_ROOT", "").strip()
        if env:
            return Path(env).expanduser().resolve()

        for parent in _PLUGIN_DIR.parents:
            if (parent / "main.py").is_file():
                return parent

        return _PLUGIN_DIR.parent.parent

    def _resolve(self, raw: str) -> Path:
        """把配置里的相对路径按框架根目录解析（绝对路径原样用）。"""

        path = Path(str(raw or "").strip()).expanduser()
        if not path.is_absolute():
            path = (self._framework_root() / path).resolve()
        return path

    def _cookie_path(self, config: BiliWhaleConfig) -> Path:
        return self._resolve(config.plugin.cookie_file)

    def _load_login(self, config: BiliWhaleConfig) -> None:
        path = self._cookie_path(config)
        try:
            cookies = load_cookies(path)
            client = BiliClient(cookies)
            runtime.set_client(client)
            logger.info(
                f"bili_whale: 登录态已装载（{path.name}，uid={client.uid or '未知'}）"
            )
        except BiliError as exc:
            runtime.set_client(None, str(exc))
            logger.warning(
                f"bili_whale: 登录态没装载成功——{exc}｜"
                f"期望路径：{path}。工具会如实说明「需要重新导出 cookie」，不会静默失败。"
            )
        except Exception as exc:  # noqa: BLE001 - 任何解析异常都不该拖垮加载
            runtime.set_client(None, f"{type(exc).__name__}: {exc}")
            logger.warning(f"bili_whale: 读取登录态异常——{type(exc).__name__}: {exc}")

    def _check_mcp(self, config: BiliWhaleConfig) -> None:
        """查一眼 playwright MCP 在不在；不在就提醒，不报错。"""

        if not config.plugin.check_mcp:
            return

        target = (config.plugin.mcp_server_name or "playwright").strip()
        try:
            from src.core.managers import get_mcp_manager

            manager = get_mcp_manager()
            metas = manager.get_connected_server_metadata() if manager else []
            names = {str(getattr(m, "server_name", "") or "").strip() for m in metas}
        except Exception as exc:  # noqa: BLE001 - 框架版本差异不该阻塞插件
            logger.info(
                f"bili_whale: 查不到 MCP 状态（{type(exc).__name__}: {exc}），"
                f"跳过检查；登录态过期时请手动用 playwright MCP 重新导出一份 cookies。"
            )
            return

        if target in names:
            logger.info(f"bili_whale: 检测到 MCP 服务「{target}」，登录态过期可直接重新导出")
        else:
            logger.warning(
                f"bili_whale: 没检测到 MCP 服务「{target}」（当前已连接："
                f"{'、'.join(sorted(n for n in names if n)) or '无'}）。"
                f"这不影响现在的查询与写入；只在 cookies 过期那天会需要它——"
                f"届时请在 config/mcp.toml 里启用 [mcp.stdio_servers.{target}]，"
                f"打开 B 站后重新导出一份登录态。"
            )

    # ------------------------------------------------------------ 私信巡查

    def _start_chat_watch(self, config: BiliWhaleConfig) -> None:
        """按配置起私信自动巡查（默认关；没有登录态就不起）。"""

        chat = config.chat
        if not chat.enabled:
            logger.info(
                "bili_whale: 自动查私信没开（[chat] enabled = false）——"
                "后台不轮询，需要时用 bili_query action=chat 现查。"
            )
            return

        client = runtime.get_client()
        if client is None:
            logger.warning(
                "bili_whale: 自动查私信要登录态，但启动时没装载成功——巡查不启动。"
            )
            return

        watcher = ChatWatcher(
            client,
            idle_interval=int(chat.idle_interval),
            burst_interval=int(chat.burst_interval),
            burst_checks=int(chat.burst_checks),
            feed_path=self._resolve(chat.feed_file),
            feed_keep=int(chat.feed_keep),
            max_sessions=int(chat.max_sessions),
        )
        runtime.set_watcher(watcher)
        logger.info(
            f"bili_whale: 自动查私信已开——平时每 {watcher.idle}s 一次，"
            f"收到新私信转 {watcher.burst}s、最多 {watcher.checks} 次"
        )

        try:
            task_info = get_task_manager().create_task(
                self._register_chat_watch(float(chat.tick_seconds)),
                name="bili_whale_chat_watch_register",
                daemon=True,
            )
            self._watch_task_id = task_info.task_id
        except Exception as error:  # noqa: BLE001 - 注册失败不影响查询
            logger.warning(f"bili_whale: 排队注册私信巡查失败：{error}")

    async def _register_chat_watch(self, tick_seconds: float) -> None:
        """等调度器就绪后注册巡查节拍。"""

        interval = max(1.0, float(tick_seconds or 10.0))
        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 没有调度器就别注册
            logger.warning(f"bili_whale: 调度器不可用，私信巡查没注册：{error}")
            return

        for _ in range(40):
            try:
                schedule_id = await scheduler.create_schedule(
                    callback=self._chat_watch_tick,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=_WATCH_TASK_NAME,
                    force_overwrite=True,
                )
            except RuntimeError:
                # 调度器还没 start()，等一下再试
                await asyncio.sleep(0.5)
                continue
            except Exception as error:  # noqa: BLE001 - 反复失败就放弃
                logger.warning(f"bili_whale: 注册私信巡查失败：{error}")
                await asyncio.sleep(2.0)
                continue
            self._watch_schedule_id = schedule_id
            logger.info(f"bili_whale: 私信巡查已注册（节拍 {interval}s）")
            return

        logger.warning("bili_whale: 等调度器就绪超时，私信巡查没注册")

    async def _chat_watch_tick(self) -> None:
        """调度器回调：交给巡查器判断这一拍要不要真查。"""

        watcher = runtime.get_watcher()
        if watcher is None:
            return
        try:
            await watcher.tick()
        except Exception as error:  # noqa: BLE001 - 巡查异常不打断调度
            logger.warning(f"bili_whale: 私信巡查异常：{error}")

    async def _stop_chat_watch(self) -> None:
        """取消注册任务、移除调度、清掉巡查器。"""

        task_id = self._watch_task_id
        self._watch_task_id = None
        if task_id:
            try:
                get_task_manager().cancel_task(task_id)
            except Exception:  # noqa: BLE001 - 任务可能已结束
                pass

        schedule_id = self._watch_schedule_id
        self._watch_schedule_id = None
        try:
            scheduler = get_unified_scheduler()
        except Exception:  # noqa: BLE001 - 调度器没起来就不用清理
            scheduler = None
        if scheduler is not None:
            found = None
            try:
                found = await scheduler.find_schedule_by_name(_WATCH_TASK_NAME)
            except Exception:  # noqa: BLE001
                found = None
            for target in [item for item in (schedule_id, found) if item]:
                try:
                    await scheduler.remove_schedule(target)
                except Exception:  # noqa: BLE001 - 卸载阶段尽力清理
                    pass

        runtime.set_watcher(None)


__all__ = ["BiliWhalePlugin"]
