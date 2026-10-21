"""运行时单例：把加载好的 BiliClient / 私信巡查器挂在这里给工具层取用。

不放在 :mod:`plugin` 里，是为了让 :mod:`tools` 不必反向 import 入口模块
（入口模块导入 tools 注册组件，tools 再导入入口就成了循环）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .client import BiliClient

if TYPE_CHECKING:
    from .chat_watch import ChatWatcher

_client: BiliClient | None = None
_login_error: str = ""
_watcher: "ChatWatcher | None" = None


def set_client(client: BiliClient | None, error: str = "") -> None:
    """装载（或清空）全局客户端。"""

    global _client, _login_error
    _client = client
    _login_error = error


def get_client() -> BiliClient | None:
    return _client


def login_error() -> str:
    return _login_error


def set_watcher(watcher: "ChatWatcher | None") -> None:
    """装载（或清空）私信巡查器；插件没开自动查私信时这里是 None。"""

    global _watcher
    _watcher = watcher


def get_watcher() -> "ChatWatcher | None":
    return _watcher


def reset() -> None:
    set_client(None, "")
    set_watcher(None)


__all__ = ["set_client", "get_client", "login_error", "set_watcher", "get_watcher", "reset"]
