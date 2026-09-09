"""memory_local 插件：本地兜底记忆提供者（data/<uid>.json 内的 memories 列表 +
LLM 定期提炼，委托 memory.py 库）。提炼逻辑即原 bot.py 的 maybe_extract。"""

import logging

import memory as mem

log = logging.getLogger("cyber-gf.memory.local")

requires = ["llm", "sessions"]
provides = ["memory"]


class LocalMemoryService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._every = cfg.memory_every
        self._model = cfg.llm_model
        self._extracting: set[int] = set()

    def local_memories(self, user_id: int) -> list[str]:
        return list(self._ctx.inject("sessions").get_store(user_id)["memories"])

    async def recall(self, query: str, user_id: int | None = None) -> str:
        """本地版没有语义检索，直接返回该用户的本地记忆文本块。"""
        if user_id is None:
            return ""
        return "\n".join(f"- {m}" for m in self.local_memories(user_id))

    async def record_turn(self, user_id: int, user_text: str, reply: str) -> None:
        """纯本地模式：记录即触发定期提炼检查（频率由 maybe_extract 内部把控）。"""
        self._ctx.create_task(self.maybe_extract(user_id))

    async def maybe_extract(self, user_id: int) -> None:
        sessions = self._ctx.inject("sessions")
        store = sessions.get_store(user_id)
        hist = store["history"]
        if len(hist) < self._every * 2 or len(hist) % (self._every * 2) != 0:
            return
        if user_id in self._extracting:
            return
        self._extracting.add(user_id)
        try:
            llm = self._ctx.inject("llm")
            store["memories"] = await mem.extract(
                llm, self._model, store["memories"], hist[-self._every * 2 :]
            )
            mem.save(user_id, store)
            log.info("memories updated for %s: %d items", user_id, len(store["memories"]))
        except Exception:
            log.exception("memory extract failed")
        finally:
            self._extracting.discard(user_id)


def apply(ctx) -> None:
    ctx.inject("config")
    ctx.provide("memory", LocalMemoryService(ctx))
