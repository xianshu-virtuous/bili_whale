"""把抽出来的帧交给框架的视觉模型，换回一段人话。

为什么要有这一层
----------------
抽帧截图存成文件，对**人**有用，对**模型**没用——它看不到磁盘。要让 bot 真正
「知道画面里是什么」，得把图送进多模态模型。好消息是实例里早就配好了：

    [model_tasks.video]
    model_list = ["Qwen/Qwen3-VL-32B-Instruct"]

所以这里只是搬砖：拿模型集 → 塞几张图 + 一句要求 → 收文字。失败一律返回空串，
由调用方降级为「只给路径」——绝不因为模型挂了让整次看视频失败。

设计约束
--------
- **不污染对话上下文**：这是一次独立的 `request_name="bili_watch"` 请求，不进主 chat；
- **token 花在插件内部**：每帧 2MB 的 png 会先在插件里压成小图再送，不占对话预算；
- **描述要的是「发生了什么」**，不是画面美术评价：prompt 里明确要求按时间顺序、
  只讲能看见的事实（人物/文字/图表/动作）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import LLMPayload, ROLE, Image, Text

logger = get_logger("bili_whale.vision")

#: 送的帧越小越省 token；宽 720 足够看清字幕和图表
MAX_SIDE = 720


def _shrink(path: Path) -> Path:
    """把帧压到 MAX_SIDE 以内（有 Pillow 就压，没有就原图）。

    CDP 截图 + 2 倍放大后单帧能到 3~4MB，直接送模型既慢又贵。
    """

    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            width, height = img.size
            scale = MAX_SIDE / max(width, height)
            if scale < 1:
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                out = img.convert("RGB").resize(new_size, PILImage.LANCZOS)
                small = path.with_name(path.stem + "-small.jpg")
                out.save(small, "JPEG", quality=80)
                return small
    except Exception as exc:  # noqa: BLE001 - 压缩失败就用原图
        logger.debug(f"压缩帧失败（用原图）：{exc}")
    return path


SYSTEM_PROMPT = (
    "你在看一段 B 站视频的截图。用户会按时间顺序给你 N 张画面。\n"
    "你的任务：只根据画面里**确实能看到的东西**，按时间顺序讲清楚发生了什么。\n"
    "重点抓：人物在做什么、画面上的文字（标题/字幕/图表数字/店招）、场景变化、"
    "图表在表达什么趋势。\n"
    "不要评价画面好不好看，不要猜视频主题以外的东西，不要编造没看到的内容。\n"
    "每张用一句话说清（不超过 45 字），格式「第1张：…；第2张：…」。\n"
    "如果相邻两张几乎一样，第二张就说「画面延续，无新信息」——别凑字数。"
)


def _extract_text(response: Any) -> str:
    """从响应里抠纯文本（兼容 str / list 两种 message 形态）。"""

    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message")
    if message is None:
        return ""
    if isinstance(message, str):
        return message.strip()
    parts: list[str] = []
    for item in message if isinstance(message, (list, tuple)) else []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


async def _describe_batch(
    batch: list[tuple[int, Path]],
    *,
    title: str,
    start_index: int,
    total: int,
    model_set: Any,
    max_tokens: int,
    temperature: float,
) -> str:
    """描述一批帧（默认 3 张）。返回这一批的文字，失败返回空串。"""

    try:
        request = llm_api.create_llm_request(model_set, request_name="bili_watch")

        # 批内每张都要标上「全局第几张」，否则多批回来的编号会各说各的
        range_desc = (
            f"这一批是第 {start_index}~{start_index + len(batch) - 1} 张"
            f"（全片共 {total} 张）。"
            if len(batch) > 1
            else f"这一张是第 {start_index} 张（全片共 {total} 张）。"
        )
        contents: list[Any] = [
            Text(
                f"视频标题：{title or '（未知）'}\n"
                f"{range_desc}下面按时间顺序给你 {len(batch)} 帧画面，"
                f"请说明这一段时间里画面发生了什么。"
            )
        ]
        for offset, (at, path) in enumerate(batch):
            index = start_index + offset
            contents.append(Text(f"第 {index} 张（第 {at // 60:02d}:{at % 60:02d}）："))
            contents.append(Image(str(path)))

        system_prompt = SYSTEM_PROMPT.replace(
            "格式「第1张：…；第2张：…」",
            f"格式「第{start_index}张：…；第{start_index + 1}张：…」（编号接着全局排）",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, contents))

        response = await request.send(stream=False)
        await response
        return _extract_text(response)
    except Exception as exc:  # noqa: BLE001 - 单批失败不影响其他批
        logger.warning(f"描述第 {start_index} 张这批失败（{type(exc).__name__}）：{exc}")
        return ""


async def describe_frames(
    frames: list[tuple[int, str]],
    *,
    title: str = "",
    task_name: str = "video",
    fallback_name: str = "",
    batch_size: int = 3,
    max_tokens: int = 1200,
    temperature: float = 0.3,
) -> str:
    """把 ``[(秒数, 图片路径), ...]`` 交给视觉模型，返回描述文本。

    **分批送**：一次塞 10 张图进去，请求体又大又慢，模型还容易只抓前面几张、
    后面敷衍过去。实测拆成每批 3 张，每批都说得具体得多，总耗时也没明显变长
    （单次请求更大反而更容易超时）。

    **批级全有或全无的兜底**：``fallback_name`` 非空时，某一批没拿到描述就用它
    直接点名重试一次。这是为了绕开 round_robin 没有失败规避这件事——
    Qwen3-VL 超时那次是真失败，轮询不会自动换人，只能我们自己兜。

    失败返回空字符串（调用方降级），不抛异常。
    """

    usable: list[tuple[int, Path]] = []
    for at, raw in frames:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            usable.append((at, _shrink(path)))
    if not usable:
        return ""

    try:
        model_set = llm_api.get_model_set_by_task(task_name)
    except Exception as exc:  # noqa: BLE001 - 模型集解析失败就降级
        logger.warning(f"拿不到视觉模型集（task={task_name}）：{exc}")
        return ""
    if not model_set:
        logger.warning(f"模型任务 {task_name!r} 没有配模型，跳过画面描述")
        return ""

    # 兜底模型集按需解析一次即可
    backup_set: Any = None
    backup_ready = False

    def _backup() -> Any:
        nonlocal backup_set, backup_ready
        if backup_ready:
            return backup_set
        backup_ready = True
        name = (fallback_name or "").strip()
        if not name:
            return None
        try:
            backup_set = llm_api.get_model_set_by_name(name)
            logger.info(f"画面描述兜底模型就绪：{name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"兜底模型 {name!r} 解析失败：{exc}")
            backup_set = None
        return backup_set

    size = max(1, min(int(batch_size or 3), 6))
    total = len(usable)
    parts: list[str] = []
    for start in range(0, total, size):
        batch = usable[start : start + size]
        text = await _describe_batch(
            batch,
            title=title,
            start_index=start + 1,
            total=total,
            model_set=model_set,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not text:
            spare = _backup()
            if spare:
                logger.info(f"第 {start + 1} 张这批改用兜底模型重试")
                text = await _describe_batch(
                    batch,
                    title=title,
                    start_index=start + 1,
                    total=total,
                    model_set=spare,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
        if text:
            parts.append(text)
    if not parts:
        logger.warning("所有批次都没拿到描述（含兜底）")
        return ""
    return "\n".join(parts)


__all__ = ["describe_frames"]
