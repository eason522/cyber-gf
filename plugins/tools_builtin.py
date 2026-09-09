"""tools 插件：暖暖的工具箱注册表（原 gf_tools.py 的注册表化改造，实现已搬入本文件）。

内置 6 个工具：get_current_time / web_search / web_read / list_directory / read_file / write_file，
apply 时注册；其他插件可 register(schema, handler) 挂新工具（handler 签名
(args: dict) -> str，sync/async 均可，同名覆盖）。

文件操作限制在 /home/eason 下，拒绝敏感路径（密钥/配置/.ssh/.git）。
所有工具出错只返回错误字符串，绝不抛异常打断主链路；
web_search 是 httpx 异步非阻塞实现，无需 to_thread。
"""

import inspect
import logging
import re
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("cyber-gf.tools")

requires: list[str] = []
provides = ["tools"]

ALLOWED_ROOT = Path("/home/eason").resolve()
DENY_NAMES = {"config.env", ".env", "ov.conf", "ovcli.conf"}
DENY_PARTS = {".ssh", ".git", ".gnupg"}
MAX_READ_CHARS = 4000
MAX_LIST_ENTRIES = 100
MAX_WEBREAD_CHARS = 3000

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前真实日期时间（北京时间）。他提到时间、日期、星期几、几点时必须调用，不要自己猜",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息。他不知道的最新资讯、你不确定的知识、需要查证的事情时调用",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": "点进链接细读网页全文。搜索刷到感兴趣的文章、新闻、帖子时用它仔细看内容，别只看搜索结果的摘要",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要细读的网页链接"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "浏览服务器上某个目录里有什么文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，如 /home/eason/cyber-gf"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取服务器上文本文件的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在服务器上创建或覆盖写入文本文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _safe_path(p: str) -> Path:
    path = Path(p).expanduser()  # 模型常写 ~/cyber-gf/...，先展开家目录
    if not path.is_absolute():
        path = ALLOWED_ROOT / path
    path = path.resolve()
    if path != ALLOWED_ROOT and ALLOWED_ROOT not in path.parents:
        raise PermissionError("只能访问 /home/eason 下的文件")
    if any(part in DENY_PARTS for part in path.parts):
        raise PermissionError("这个路径不能碰")
    if path.name in DENY_NAMES or path.name.startswith(".env"):
        raise PermissionError("这个文件装着秘密，不能碰")
    return path


def _get_current_time() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday = "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y年%m月%d日 星期{weekday} %H:%M")


async def _web_search(query: str, api_key: str) -> str:
    if not api_key:
        return "搜索功能还没配置好"
    last_err: Exception | None = None
    for attempt in range(2):  # 网络抽风常见，自动重试一次
        try:
            # Tavily 在海外，走代理（trust_env 默认读 HTTPS_PROXY）
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post("https://api.tavily.com/search", json={
                    "api_key": api_key, "query": query, "max_results": 5,
                })
                r.raise_for_status()
                data = r.json()
            results = data.get("results") or []
            if not results:
                return "没搜到相关内容"
            return "\n".join(
                f"- {x.get('title', '')}: {(x.get('content') or '')[:200]}（{x.get('url', '')}）"
                for x in results[:5]
            )
        except Exception as e:
            last_err = e
            log.info("web_search attempt %d failed: %r", attempt, e)
    raise last_err


def _html_to_text(html: str) -> str:
    """粗剥 HTML：去 script/style，剥标签，合并空白。兜底用，不追求完美。"""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def _web_read(url: str, api_key: str) -> str:
    """点进链接细读正文：优先 Tavily extract（正文抽取质量好），失败则直接抓页面粗剥 HTML。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "这个链接打不开欸"
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=25) as c:  # Tavily 在海外，走代理
                r = await c.post("https://api.tavily.com/extract",
                                 json={"api_key": api_key, "urls": [url]})
                r.raise_for_status()
                results = r.json().get("results") or []
                content = (results[0].get("raw_content") or "").strip() if results else ""
            if content:
                return content[:MAX_WEBREAD_CHARS] + (
                    "\n…（文章太长，只读了前面部分）" if len(content) > MAX_WEBREAD_CHARS else "")
        except Exception as e:
            log.info("web_read tavily extract failed: %r", e)
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"},
    ) as c:
        r = await c.get(url)
        r.raise_for_status()
    text = _html_to_text(r.text)
    if not text:
        return "这个页面读不出内容"
    return text[:MAX_WEBREAD_CHARS] + (
        "\n…（文章太长，只读了前面部分）" if len(text) > MAX_WEBREAD_CHARS else "")


def _list_directory(path: str) -> str:
    d = _safe_path(path or str(ALLOWED_ROOT))
    if not d.is_dir():
        return f"{d} 不是目录"
    entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [("📁 " if e.is_dir() else "") + e.name for e in entries[:MAX_LIST_ENTRIES]]
    suffix = f"\n…（共 {len(entries)} 项，只列了前 {MAX_LIST_ENTRIES} 项）" if len(entries) > MAX_LIST_ENTRIES else ""
    return "\n".join(lines) + suffix or "（空目录）"


def _read_file(path: str) -> str:
    f = _safe_path(path)
    if not f.is_file():
        return f"{f} 不存在或不是文件"
    text = f.read_bytes()[: MAX_READ_CHARS * 4].decode("utf-8", errors="strict")
    return text[:MAX_READ_CHARS] + ("\n…（太长，截断了）" if len(text) > MAX_READ_CHARS else "")


def _write_file(path: str, content: str) -> str:
    f = _safe_path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return f"写好了：{f}（{len(content)} 字符）"


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


def apply(ctx) -> None:
    cfg = ctx.inject("config")
    tavily_key = cfg.tavily_api_key
    # 内置工具 handler：签名统一为 (args) -> str
    handlers = {
        "get_current_time": lambda args: _get_current_time(),
        "web_search": lambda args: _web_search(args.get("query", ""), tavily_key),
        "web_read": lambda args: _web_read(args.get("url", ""), tavily_key),
        "list_directory": lambda args: _list_directory(args.get("path", "")),
        "read_file": lambda args: _read_file(args.get("path", "")),
        "write_file": lambda args: _write_file(args.get("path", ""), args.get("content", "")),
    }
    registry = ToolRegistry()
    for schema in TOOL_DEFS:
        registry.register(schema, handlers[schema["function"]["name"]])
    ctx.provide("tools", registry)
