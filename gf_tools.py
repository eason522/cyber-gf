"""暖暖的工具箱：查时间、联网搜索（Tavily）、浏览/读写服务器文件。

文件操作限制在 /home/eason 下，拒绝敏感路径（密钥/配置/.ssh/.git）。
所有工具出错只返回错误字符串，绝不抛异常打断主链路。
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("cyber-gf.tools")

ALLOWED_ROOT = Path("/home/eason").resolve()
DENY_NAMES = {"config.env", ".env", "ov.conf", "ovcli.conf"}
DENY_PARTS = {".ssh", ".git", ".gnupg"}
MAX_READ_CHARS = 4000
MAX_LIST_ENTRIES = 100

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


def get_current_time() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday = "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y年%m月%d日 星期{weekday} %H:%M")


async def web_search(query: str) -> str:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return "搜索功能还没配置好"
    # Tavily 在海外，走代理（trust_env 默认读 HTTPS_PROXY）
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://api.tavily.com/search", json={
            "api_key": key, "query": query, "max_results": 5,
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


def list_directory(path: str) -> str:
    d = _safe_path(path or str(ALLOWED_ROOT))
    if not d.is_dir():
        return f"{d} 不是目录"
    entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [("📁 " if e.is_dir() else "") + e.name for e in entries[:MAX_LIST_ENTRIES]]
    suffix = f"\n…（共 {len(entries)} 项，只列了前 {MAX_LIST_ENTRIES} 项）" if len(entries) > MAX_LIST_ENTRIES else ""
    return "\n".join(lines) + suffix or "（空目录）"


def read_file(path: str) -> str:
    f = _safe_path(path)
    if not f.is_file():
        return f"{f} 不存在或不是文件"
    text = f.read_bytes()[: MAX_READ_CHARS * 4].decode("utf-8", errors="strict")
    return text[:MAX_READ_CHARS] + ("\n…（太长，截断了）" if len(text) > MAX_READ_CHARS else "")


def write_file(path: str, content: str) -> str:
    f = _safe_path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return f"写好了：{f}（{len(content)} 字符）"


async def run(name: str, args: dict) -> str:
    try:
        if name == "get_current_time":
            return get_current_time()
        if name == "web_search":
            return await web_search(args.get("query", ""))
        if name == "list_directory":
            return list_directory(args.get("path", ""))
        if name == "read_file":
            return read_file(args.get("path", ""))
        if name == "write_file":
            return write_file(args.get("path", ""), args.get("content", ""))
        return f"没有 {name} 这个工具"
    except Exception as e:
        log.warning("tool %s failed: %s", name, e)
        return f"工具出错了：{e}"
