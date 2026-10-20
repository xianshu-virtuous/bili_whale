"""bili_whale 插件包：让深深像主人一样用 B 站。

插件入口在 :mod:`plugin`，由加载器以 ``bili_whale.plugin`` 之名执行，因此这里
**不要**导入 ``plugin`` 里的东西（父包初始化时入口模块还没跑完，会变成循环导入）。

本包分工：

- :mod:`config` —— 插件配置（登录态路径、写操作开关、MCP 检查）；
- :mod:`client` —— 纯 HTTP 的 B 站客户端（wbi 签名、各接口，同步实现，工具层丢线程池）；
- :mod:`tools` —— 两个工具：``bili_query``（只读）与 ``bili_action``（写，受开关约束）；
- :mod:`plugin` —— 入口：注册组件、加载登录态、检查 MCP、启动自检。
"""

__all__: list[str] = []
