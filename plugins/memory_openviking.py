"""memory_openviking 插件：OpenViking 长期记忆提供者，override 本地 "memory" 服务。

语义与 bot.py 现有调用点完全一致（委托 ov_memory.py 库）：
- recall：ov_memory.recall 内部自带健康检查 + 30s 超时降级；为空时回退本地记忆块。
- record_turn：ov_memory.record_turn 自带"每 MEMORY_EVERY 轮 commit + pending 缓冲 +
  OV 挂则放回缓冲"语义；OV 不健康时额外触发本地提炼兜底（原 bot.py:467-469）。
"""

import ov_memory

requires = ["memory"]
provides = ["memory"]


class OpenVikingMemoryService:
    def __init__(self, ctx, local):
        self._ctx = ctx
        self._local = local

    def local_memories(self, user_id: int) -> list[str]:
        return self._local.local_memories(user_id)

    async def recall(self, query: str, user_id: int | None = None) -> str:
        items = await ov_memory.recall(query)
        if items:
            return "\n".join(f"- {m}" for m in items)
        if user_id is not None:  # OV 不可用/无结果，回退本地记忆
            return await self._local.recall(query, user_id)
        return ""

    async def record_turn(self, user_id: int, user_text: str, reply: str) -> None:
        await ov_memory.record_turn(user_id, user_text, reply)
        if not await ov_memory.healthy():  # OV 不在线时用本地提炼兜底
            self._ctx.create_task(self._local.maybe_extract(user_id))

    async def maybe_extract(self, user_id: int) -> None:
        await self._local.maybe_extract(user_id)


def apply(ctx) -> None:
    ctx.inject("config")
    local = ctx.inject("memory")
    ctx.provide("memory", OpenVikingMemoryService(ctx, local), override=True)
