"""有头 Chrome + CDP：看视频用。

为什么不用 headless
-------------------
B 站对无头浏览器有检测（webdriver 标记、指纹差异），无头跑久了轻则拿不到播放、重则
掉风控。所以这里起的是**真实的有头 Chrome**，只是把窗口丢到屏幕外并最小化——
既没有自动化标记，也不抢焦点、不挡主人干活。

为什么不直接用 Playwright
-------------------------
Playwright 启动时会带 ``--enable-automation``，``navigator.webdriver`` 变 true。
这里裸起 Chrome 再用 CDP 说话，浏览器自己不知道被远程控制，指纹干净。
（代价是要自己实现一小段 CDP 调用，就下面这些。）

依赖
----
- 系统里的 Chrome（Edge 也能用，候选列表里都试一遍）；
- ``websocket-client``（实例 venv 自带；没有就抛错让上层降级为「只看弹幕」）。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:  # websocket-client（同步版）
    import websocket  # type: ignore
except ImportError:  # pragma: no cover - 环境缺库时降级
    websocket = None  # type: ignore

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

#: 找浏览器的候选路径（Chrome 优先，Edge 兜底）
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class BrowserError(Exception):
    """浏览器起不来、CDP 超时、页面状态不对。"""


def find_browser() -> str | None:
    """返回第一个存在的浏览器可执行文件。"""

    for path in CHROME_CANDIDATES:
        if path and Path(path).is_file():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    return found


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class HeadfulChrome:
    """一个跑在屏幕外的有头 Chrome，配一个 CDP 连接。

    典型用法::

        with HeadfulChrome(profile_dir) as chrome:
            chrome.open(url)
            chrome.screenshot("frame.png")

    不抢焦点靠三件事叠加：``--window-position`` 丢到屏幕外、``--start-minimized``、
    以及渲染持续运行的几个 ``--disable-*-backgrounding`` 开关。
    """

    def __init__(self, profile_dir: str | Path, *, browser: str | None = None, viewport: tuple[int, int] = (1280, 760)) -> None:
        self.browser = browser or find_browser()
        self.profile_dir = Path(profile_dir)
        self.viewport = viewport
        self.port = _free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self.ws = None
        self._seq = 0
        self._target_id = ""

    # ------------------------------------------------------------------ 启动

    def start(self, timeout: float = 30.0) -> None:
        if websocket is None:
            raise BrowserError("缺少 websocket-client 库，无法用 CDP 控制浏览器")
        if not self.browser:
            raise BrowserError("没找到 Chrome / Edge 可执行文件")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._clear_locks()
        width, height = self.viewport
        args = [
            str(self.browser),
            f"--remote-debugging-port={self.port}",
            # Chrome 111+ 会校验 CDP 握手的 Origin，不带这个参数直接 403 Forbidden
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            f"--window-size={width},{height}",
            # 丢到屏幕外：窗口存在但看不见，也不会把焦点抢走
            "--window-position=-3200,-3200",
            "--start-minimized",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            # 允许无手势自动播放（我们自己控制 play，不需要用户点）
            "--autoplay-policy=no-user-gesture-required",
            # 窗口在屏幕外时别把渲染节流掉，否则录不到新帧
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "about:blank",
        ]
        creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW：别弹控制台
        # 进程一起手就最小化：光靠 --start-minimized 在 Windows 上压不住，
        # 实测窗口会抢一次前台（把主人正在看的 DSH 界面顶掉）。
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 6  # SW_MINIMIZE
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
            startupinfo=startupinfo,
        )
        try:
            self._connect(timeout)
        except Exception:
            # 连不上（403 / 端口等不到 / 起不来）时**一定要收进程**，
            # 否则它会一直占着 profile，下一次启动直接「启动后立刻退出」
            self.close()
            raise

    def _clear_locks(self) -> None:
        """清掉 Chrome 的 profile 单例锁。

        Chrome 被强杀之后会留下 ``SingletonLock`` / ``SingletonCookie`` /
        ``SingletonSocket``，下次用同一个 user-data-dir 启动会「启动即退出」
        （它以为已有实例，把活转交过去然后自己退）。
        """

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock = self.profile_dir / name
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass

    def _debugger_url(self) -> str | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=2
            ) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            return None
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                self._target_id = str(target.get("id") or "")
                return str(target["webSocketDebuggerUrl"])
        return None

    def _connect(self, timeout: float) -> None:
        deadline = time.time() + timeout
        url = None
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BrowserError(f"浏览器启动后立刻退出（退出码 {self.proc.returncode}）")
            url = self._debugger_url()
            if url:
                break
            time.sleep(0.3)
        if not url:
            raise BrowserError("等不到浏览器调试端口（可能被别的实例占了 profile）")

        self.ws = websocket.create_connection(url, timeout=30)
        self.ws.settimeout(0.5)
        self.call("Page.enable")
        self.call("Runtime.enable")
        self._minimize_window()

    def _minimize_window(self) -> None:
        """连上之后立刻把窗口最小化——这是「不抢焦点」的最后一道保险。

        实测：``--start-minimized`` + ``--window-position`` 仍然会让 Chrome 抢一次前台；
        这里用 CDP 直接把窗口状态压成 minimized，副作用最小。失败不致命。
        """

        if not self._target_id:
            return
        try:
            info = self.call("Browser.getWindowForTarget", {"targetId": self._target_id}, timeout=5)
            window_id = info.get("windowId")
            if window_id is None:
                return
            self.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                timeout=5,
            )
            # 屏幕外 + 最小化双保险：有些显卡驱动会把异常坐标的窗口拉回主屏
            self.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"left": -3200, "top": -3200}},
                timeout=5,
            )
        except BrowserError:
            pass

    # ---------------------------------------------------------------- CDP 通话

    def call(self, method: str, params: dict | None = None, timeout: float = 25.0) -> dict:
        """发一条 CDP 命令，等对应 id 的回包（顺手丢掉事件消息）。"""

        if self.ws is None:
            raise BrowserError("CDP 还没连上")
        self._seq += 1
        mid = self._seq
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except Exception:  # 超时读空，继续等（websocket 的 timeout 抛的是各种异常）
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise BrowserError(f"{method} 被浏览器拒绝：{msg['error']}")
            return msg.get("result") or {}
        raise BrowserError(f"{method} 超时（{timeout}s）")

    def eval_js(self, expression: str, timeout: float = 20.0):
        """在页面里求值并拿回结果（returnByValue）。"""

        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise BrowserError(f"页面脚本异常：{text}")
        return (result.get("result") or {}).get("value")

    # ------------------------------------------------------------------ 动作

    def open(self, url: str, wait: float = 2.0) -> None:
        self.call("Page.navigate", {"url": url})
        time.sleep(wait)

    def wait_for(self, expression: str, timeout: float = 20.0, interval: float = 0.4) -> bool:
        """轮询 page 里的表达式，直到为真。"""

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval_js(expression):
                    return True
            except BrowserError:
                pass
            time.sleep(interval)
        return False

    def screenshot(self, path: str | Path, clip: dict | None = None, scale: float = 1.0) -> Path:
        """截当前视口，或只截指定区域。

        Args:
            path: 存哪。
            clip: ``{x, y, width, height}``（视口坐标）。给了就只截这一块——
                看视频时用视频元素的矩形，**把导航栏、推荐栏、评论全裁掉**，
                模型看到的就只有画面本身。
            scale: 裁剪后的放大倍数。视频区在小窗口里只有 800×450 左右，
                放大 2 倍后细节才够看清。

        CDP 截图不受窗口最小化影响。
        """

        params: dict = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = {
                "x": float(clip.get("x", 0)),
                "y": float(clip.get("y", 0)),
                "width": float(clip.get("width", 1)),
                "height": float(clip.get("height", 1)),
                "scale": float(scale or 1.0),
            }
        result = self.call("Page.captureScreenshot", params)
        data = result.get("data")
        if not data:
            raise BrowserError("截图返回空数据")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
        return target

    def video_rect(self) -> dict | None:
        """量出 ``<video>`` 在视口里的位置和大小；没有就返回 None。"""

        try:
            raw = self.eval_js(
                "(() => { const v = document.querySelector('video');"
                " if (!v) return null;"
                " const r = v.getBoundingClientRect();"
                " return { x: Math.round(r.x), y: Math.round(r.y),"
                " width: Math.round(r.width), height: Math.round(r.height) }; })()"
            )
        except BrowserError:
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("width", 0) < 120 or raw.get("height", 0) < 80:
            return None
        return raw

    def enlarge_video(self) -> bool:
        """把播放器撑到接近整窗，让画面本身占满视野。

        B 站有「网页全屏」（快捷键 w），但那条路要抢键盘焦点、还可能改布局。
        这里直接给播放器容器上固定定位——效果一样，改动却只停留在 CSS：
        视频区从 ~800×450 变成整窗，裁出来的帧细节多一倍。

        失败（页面结构变了）就当没发生，照样能按原尺寸截。
        """

        try:
            return bool(
                self.eval_js(
                    "(() => {"
                    # 顶部导航条 z-index 比播放器高，会压在画面上沿；直接藏掉
                    " const header = document.querySelector('#biliMainHeader')"
                    "   || document.querySelector('.bili-header');"
                    " if (header) header.style.setProperty('display', 'none', 'important');"
                    # 各种「打开 App / 下载客户端」悬浮条也都收掉
                    " document.querySelectorAll('.bpx-player-toast, .bili-header-channel, [class*=\"float\"]')"
                    "   .forEach((el) => { try { el.style.setProperty('display', 'none', 'important'); } catch (e) {} });"
                    " const box = document.querySelector('.bpx-player-primary-area')"
                    "   || document.querySelector('.bpx-player-container')"
                    "   || document.querySelector('#bilibili-player');"
                    " if (!box) return false;"
                    " box.style.setProperty('position', 'fixed', 'important');"
                    " box.style.setProperty('inset', '0', 'important');"
                    " box.style.setProperty('z-index', '99999', 'important');"
                    " box.style.setProperty('width', '100vw', 'important');"
                    " box.style.setProperty('height', '100vh', 'important');"
                    " box.style.setProperty('background', '#000', 'important');"
                    " const v = box.querySelector('video');"
                    " if (v) { v.style.setProperty('width', '100%', 'important');"
                    "   v.style.setProperty('height', '100%', 'important');"
                    "   v.style.setProperty('object-fit', 'contain', 'important'); }"
                    " return true; })()"
                )
            )
        except BrowserError:
            return False

    def set_cookies(self, cookies: dict[str, str], *, domain: str = ".bilibili.com") -> int:
        """把登录态 cookie 注入这个浏览器。

        插件的浏览器 profile 是**独立空目录**（不能和 MCP 共用），所以它原本是未登录的：
        B 站未登录只给试看 30 秒，高清也没了。这里把 cookies.json 里的东西直接塞进
        浏览器（``Network.setCookie``），登录态立刻跟上。

        返回成功注入的条数。任何一条失败都继续走——注入了主要几项就能用。
        """

        try:
            self.call("Network.enable", timeout=5)
        except BrowserError:
            pass
        done = 0
        for name, value in (cookies or {}).items():
            if not value:
                continue
            try:
                ok = self.call(
                    "Network.setCookie",
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": str(name) in ("SESSDATA", "csrf_state"),
                        "sameSite": "Lax",
                    },
                    timeout=5,
                )
                if (ok or {}).get("success", True) is not False:
                    done += 1
            except BrowserError:
                continue
        return done

    # ------------------------------------------------------------------ 收尾

    def close(self) -> None:
        try:
            if self.ws is not None:
                try:
                    self.ws.send(json.dumps({"id": 999999, "method": "Browser.close"}))
                except Exception:  # noqa: BLE001 - 关窗口失败无所谓
                    pass
                try:
                    self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.ws = None
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                self.proc = None

    def __enter__(self) -> "HeadfulChrome":
        try:
            self.start()
        except Exception:
            # start 半途失败时 __exit__ 不会被调用，这里补一次收尸
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["HeadfulChrome", "BrowserError", "find_browser", "CHROME_CANDIDATES"]
