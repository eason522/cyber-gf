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

# 冲浪：空闲时她自己上网刷新闻/八卦，刷到感兴趣的点进去细读，连心情一起写进小本本
SURF_PROMPT = (
    "（系统提示：现在是空闲时间，你可以自己上网冲浪啦。"
    "看看你最近在追的明星、在嗑的八卦有什么新动态，或者去发现点新的好玩的东西——"
    "娱乐新闻、社会热点都可以。先用 list_directory / read_file 翻翻小本本里你之前记过什么，"
    "再用 web_search 搜新内容。别只看搜索结果的摘要——刷到感兴趣的标题，"
    "就像人刷手机一样点进去，用 web_read 把那篇文章仔细读完（一次冲浪挑一两篇认真读就好，"
    "不用每篇都点）。读完如果有触动你的地方，用 write_file 写进小本本 ~/cyber-gf/tinynote/。"
    "记笔记要像写日记、写收藏备注，不是记流水账：除了发生了什么，更要写下你当时的心情——"
    "为什么戳到你、哪里戳到、你联想到了什么、下次想怎么跟他讲这件事。"
    "以后你翻看小本本时，要靠这些心情才能想起当时为什么记下它、才知道跟他分享时哪里有趣。"
    "如果没什么想看的，就只回复 NO_SURF。）"
)


class SurfService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._minutes = cfg.surf_minutes
        self._model = cfg.llm_model

    async def _surf_once(self) -> None:
        """一轮冲浪：翻小本本 → 按兴趣手账引导搜索 → 挑感兴趣的 web_read 细读 → 连心情一起记录，最多 10 轮工具调用。全程静默，不打扰他。"""
        system = self._ctx.inject("persona").system_prompt("")
        tools = self._ctx.inject("tools")
        llm = self._ctx.inject("llm")
        prompt = SURF_PROMPT
        if self._ctx.has("interests"):
            interests = self._ctx.inject("interests").get()
            if interests:
                prompt += (
                    "\n\n你最近的兴趣手账（优先围绕「长期热爱」和「最近上头」刷，"
                    "也留点时间翻翻「想探索的新领域」，别让兴趣越刷越窄）：\n" + interests
                )
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        for i in range(10):
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
