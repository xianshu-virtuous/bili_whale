"""bili_whale 插件配置。

配置文件默认路径：``config/plugins/bili_whale/config.toml``

三组开关，默认值都是「装上就能读、写操作先关着」：

- ``[plugin]`` —— 登录态文件在哪、要不要检查 MCP；
- ``[actions]`` —— 写操作总闸与四项细分（评论 / 弹幕 / 点赞 / 收藏）；
- ``[query]`` —— 只读工具的默认条数（省 token）。

为什么写操作默认是开的：这个插件挂在**主人自己的账号**上、也只给主人用，
装完就想让它能干活。要是装在别人的实例上，把 ``[actions] enabled`` 关掉即可——
评论和弹幕发出去就删不干净，这一点在 README 里写明了。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class BiliWhaleConfig(BaseConfig):
    """bili_whale 插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "B站账号管家（深深专用）配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """插件基础设置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用插件（关掉＝不注册任何工具）",
        )
        cookie_file: str = Field(
            default="data/bili_cookies.json",
            description=(
                "登录态文件路径（JSON，扁平 name→value）。\n"
                "相对路径按框架根目录解析。文件由 Playwright 导出的 cookies 写成，"
                "至少要有 SESSDATA / bili_jct / DedeUserID 三项。\n"
                "**不要把这个文件提交到 git**。"
            ),
        )
        check_mcp: bool = Field(
            default=True,
            description=(
                "加载时检查 playwright MCP 是否已连接。\n"
                "只影响一条提醒日志：登录态过期时，需要靠 playwright MCP 重新导出 cookies——"
                "没连上就提前提醒一句，其余功能照常。"
            ),
        )
        mcp_server_name: str = Field(
            default="playwright",
            description="要检查的 MCP 服务名（对应 config/mcp.toml 里 [mcp.stdio_servers.<名字>]）",
        )

    @config_section("actions", title="写操作开关", tag="actions")
    class ActionsSection(SectionBase):
        """写操作开关。默认全开（这个插件是给主人自己用的号）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "写操作总闸。\n"
                "``true``（默认）＝ bili_action 可以动；``false``＝ 直接拒绝任何写操作。\n"
                "默认打开是因为这个插件挂在主人自己的号上、也只给主人用；"
                "要是装在公共实例上，建议关掉。"
            ),
        )
        allow_reply: bool = Field(default=True, description="允许发视频评论")
        allow_danmaku: bool = Field(
            default=True,
            description="允许发弹幕（发出去删不掉，不想让它发就把这项关掉）",
        )
        allow_like: bool = Field(default=True, description="允许点赞 / 取消点赞（可逆）")
        allow_fav: bool = Field(default=True, description="允许收藏 / 取消收藏（可逆）")
        allow_follow: bool = Field(
            default=True,
            description=(
                "允许关注 / 取关 UP 主。\n"
                "关注会通知对方、留痕比较明显；这个插件挂在主人自己的号上，所以默认开，"
                "仍受 [actions] enabled 总闸约束。装在别人实例上时建议关掉。"
            ),
        )

    @config_section("query", title="只读规模", tag="query")
    class QuerySection(SectionBase):
        """只读工具的默认规模。"""

        history_limit: int = Field(default=12, description="history 默认返回条数（上限 30）")
        danmaku_limit: int = Field(default=40, description="看视频时抓多少条弹幕（上限 200）")
        order_todo_only: bool = Field(
            default=False,
            description="orders 默认只看未完成（方便直接盯待收货）",
        )

    @config_section("chat", title="私信自动巡查", tag="chat")
    class ChatSection(SectionBase):
        """自动查私信（默认关）。

        节奏（主人定的）：

        - 平时 **10 分钟**查一次会话列表；
        - 一旦查到对方发来新私信，切到 **10 秒**一次的高频；
        - 高频连着 **6 次**都没查到新消息（对方没再回复），退回 10 分钟一次。

        查到的内容只写日志和缓存文件，不主动往聊天流里推——需要时用
        ``bili_query action=chat`` 去读。
        """

        enabled: bool = Field(
            default=False,
            description=(
                "是否开启后台自动查私信。\n"
                "默认关：它会在后台按节奏请求 B 站接口（虽轻，但没必要人人开）。"
            ),
        )
        idle_interval: int = Field(
            default=600,
            description="平时多久查一次（秒，默认 600 = 10 分钟）",
        )
        burst_interval: int = Field(
            default=10,
            description="查到新私信后多久查一次（秒，默认 10 秒）",
        )
        burst_checks: int = Field(
            default=6,
            description=(
                "高频阶段连续多少次没查到新消息就退回低频（默认 6）。\n"
                "这期间又收到对方新消息会重新计数，继续高频。"
            ),
        )
        tick_seconds: float = Field(
            default=10.0,
            description=(
                "后台节拍（秒）：调度器按这个间隔唤醒，由内部状态机决定这一拍要不要真的请求。\n"
                "**不要小于 burst_interval**，否则高频做不到那么密。默认 10。"
            ),
        )
        feed_file: str = Field(
            default="data/bili_whale/chat_feed.json",
            description="新私信缓存文件（相对框架根目录）",
        )
        feed_keep: int = Field(
            default=60,
            description="缓存文件最多留多少条新私信（多了丢最旧的）",
        )
        max_sessions: int = Field(
            default=20,
            description="每次巡查拉多少个会话（B 站一页最多 20 上下，太多也没意义）",
        )

    @config_section("watch", title="看视频", tag="watch")
    class WatchSection(SectionBase):
        """看视频（有头浏览器抽帧）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否允许 bili_watch 起浏览器看画面。\n"
                "关掉后 bili_watch 只回视频信息 + 弹幕（照样能说明视频在讲什么）。"
            ),
        )
        frames: int = Field(
            default=0,
            description=(
                "强制抽几帧（0 ＝ 按时长自动）：\n"
                "3 分钟内 4 帧、15 分钟内 6 帧、30 分钟内 8 帧、更长 10 帧。\n"
                "填 1~12 可覆盖。位置是「一半弹幕最热的时段 + 一半均匀」选出来的。\n"
                "帧数是成对的：同一段至少两帧才看得出有没有变化。"
            ),
        )
        browser_profile: str = Field(
            default="data/bili_browser_profile",
            description=(
                "浏览器用户目录（相对框架根）。\n"
                "**独立的空目录**，不要指到主人的日常 Chrome 或 playwright MCP 的 profile 上——"
                "同一目录不能同时被两个实例占用。"
            ),
        )
        screenshot_dir: str = Field(
            default="data/bili_watch",
            description="截图存放目录（相对框架根）",
        )
        page_timeout: float = Field(
            default=25.0,
            description="等视频元素出现的超时秒数；网络慢或首次启动浏览器时可以调大",
        )
        describe: bool = Field(
            default=True,
            description=(
                "抽到帧之后，交给框架的视觉模型描述画面（返回文字，不保存图片路径给模型看）。\n"
                "用的是 model.toml 里 ``[model_tasks.video]`` 绑定的模型；关掉就只回截图路径。"
            ),
        )
        vision_task: str = Field(
            default="video",
            description="画面描述用哪个模型任务（默认 video，对应 model.toml 的 [model_tasks.video]）",
        )
        vision_fallback: str = Field(
            default="deepseek-v4-flash-vision-exp",
            description=(
                "第一批描述失败时，直接点名这个模型重试一次（留空＝不兜底）。\n"
                "为什么要它：实例的默认调度策略是 round_robin（轮询），**没有失败规避**——\n"
                "Qwen3-VL 超时那次就是真失败，轮询不会自动换人。所以在插件层兜一手，\n"
                "既不改全局策略（会影响主对话），又能保证画面描述不会整段丢掉。"
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    actions: ActionsSection = Field(default_factory=ActionsSection)
    query: QuerySection = Field(default_factory=QuerySection)
    chat: ChatSection = Field(default_factory=ChatSection)
    watch: WatchSection = Field(default_factory=WatchSection)


__all__ = ["BiliWhaleConfig"]
