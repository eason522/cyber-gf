"""tools 插件：工具注册表（原 gf_tools.py 的注册表化改造）。

5 个内置工具（get_current_time / web_search / list_directory / read_file / write_file）
在 apply 时注册，实现委托 gf_tools 库函数（安全限制不变：路径限 /home/eason 下、
拒绝 config.env/.ssh/.git 等敏感路径）。其他插件可 register(schema, handler) 挂新工具。
错误语义与 gf_tools.run 逐字一致：出错只返回错误字符串，绝不抛异常打断主链路；
web_search 本来就是 httpx 异步非阻塞实现，无需 to_thread。
"""

import inspect
import logging

import httpx

import gf_tools

log = logging.getLogger("cyber-gf.tools")

requires: list[str] = []
provides = ["tools"]


class ToolRegistry:
    def __init__(self):
        self._schemas: list[dict] = []  # 保持注册顺序（内置 5 个工具顺序同原 TOOL_DEFS）
        self._handlers: dict[str, object] = {}

    def register(self, schema: dict, handler) -> None:
        """注册工具。handler 签名 (args: dict) -> str，sync/async 均可；同名覆盖。"""
        name = schema["function"]["name"]
        if name in self._handlers:
            self._schemas = [s for s in self._schemas if s["function"]["name"] != name]
        self._schemas.append(schema)
        self._handlers[name] = handler

    def defs(self) -> list:
        """OpenAI tools schema 列表，直接拼进 chat.completions.create 的 tools 参数。"""
        return list(self._schemas)

    async def run(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"没有 {name} 这个工具"
        try:
            r = handler(args or {})
            if inspect.isawaitable(r):
                r = await r
            return r
        except Exception as e:
            log.warning("tool %s failed: %r", name, e)
            hint = "网络问题，可以换个关键词重试一次" if isinstance(e, httpx.HTTPError) else str(e)
            return f"工具出错了（{type(e).__name__}）：{hint}"


# 内置工具 handler：签名统一为 (args) -> str，委托 gf_tools 库函数
_BUILTIN_HANDLERS = {
    "get_current_time": lambda args: gf_tools.get_current_time(),
    "web_search": lambda args: gf_tools.web_search(args.get("query", "")),
    "list_directory": lambda args: gf_tools.list_directory(args.get("path", "")),
    "read_file": lambda args: gf_tools.read_file(args.get("path", "")),
    "write_file": lambda args: gf_tools.write_file(args.get("path", ""), args.get("content", "")),
}


def apply(ctx) -> None:
    ctx.inject("config")
    registry = ToolRegistry()
    for schema in gf_tools.TOOL_DEFS:
        registry.register(schema, _BUILTIN_HANDLERS[schema["function"]["name"]])
    ctx.provide("tools", registry)
