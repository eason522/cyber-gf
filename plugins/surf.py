"""surf 插件：空闲冲浪循环（原 bot.py 的 surf_loop/_surf_once，逻辑逐字保留）。

白天每隔 SURF_MINUTES 分钟让她自己上网刷新闻/八卦，新发现写进小本本（tinynote/），
聊天时由 persona 的 tinynote_block 注入上下文，她会主动分享。全程静默，不打扰他。
"""

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("cyber-gf.surf")

ACTIVE_HOURS = (8, 23)  # 深夜免打扰（北京时间）

requires = ["llm", "tools", "persona"]
provides: list[str] = []

# 冲浪：空闲时她自己上网刷新闻/八卦，新发现写进小本本
SURF_PROMPT = (
    "（系统提示：现在是空闲时间，你可以自己上网上冲浪啦。"
    "看看你最近在追的明星、在嗑的八卦有什么新动态，或者去发现点新的好玩的东西——"
    "娱乐新闻、社会热点都可以。先用 list_directory / read_file 翻翻小本本里你之前记过什么，"
    "再用 web_search 搜新内容，值得记住的用 write_file 写进小本本 ~/cyber-gf/tinynote/"
    "（可以自己维护一个冲浪笔记文件，比如最近追的星、在关注的事）。"
    "如果没什么想看的，就只回复 NO_SURF。）"
)


class SurfService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._minutes = cfg.surf_minutes
        self._model = cfg.llm_model

    async def _surf_once(self) -> None:
        """一轮冲浪：翻小本本 → 搜索 → 记录，最多 6 轮工具调用。全程静默，不打扰他。"""
        system = self._ctx.inject("persona").system_prompt("")
        tools = self._ctx.inject("tools")
        llm = self._ctx.inject("llm")
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": SURF_PROMPT},
        ]
        for i in range(6):
            resp = await llm.chat.completions.create(
                model=self._model, messages=msgs, tools=tools.defs(), tool_choice="auto",
            )
            m = resp.choices[0].message
            if not m.tool_calls:
                log.info("surf: done (%s)", (m.content or "")[:40])
                return
            msgs.append({
                "role": "assistant",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in m.tool_calls],
            })
            for c in m.tool_calls:
                try:
                    targs = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    targs = {}
                out = await tools.run(c.function.name, targs)
                log.info("surf tool: %s(%s) -> %s", c.function.name,
                         {k: str(v)[:30] for k, v in targs.items()}, out[:60])
                msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})

    async def loop(self) -> None:
        """空闲冲浪循环：白天每隔 SURF_MINUTES 分钟让她自己上网刷刷，新发现写进小本本。"""
        await asyncio.sleep(300)  # 启动后先等五分钟
        while True:
            try:
                hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
                if ACTIVE_HOURS[0] <= hour < ACTIVE_HOURS[1]:
                    await self._surf_once()
            except Exception:
                log.exception("surf error")
            finally:
                await asyncio.sleep(self._minutes * 60)


def apply(ctx) -> None:
    ctx.create_task(SurfService(ctx).loop())
