"""OpenViking 长期记忆封装：检索（find）+ 会话提交（session commit）。

所有函数在 OpenViking 不可用时优雅降级（返回空/跳过），绝不阻塞主链路。
"""

import asyncio
import logging
import os
import time
from collections import defaultdict

log = logging.getLogger("cyber-gf.ov")

OV_URL = os.getenv("OV_URL", "http://127.0.0.1:1933")
OV_API_KEY = os.getenv("OV_API_KEY", "")
COMMIT_EVERY = int(os.getenv("MEMORY_EVERY", "4"))
RECALL_TOP_K = int(os.getenv("OV_RECALL_TOP_K", "5"))

_client = None
_health: tuple[bool, float] = (False, 0.0)
_pending: dict[int, list[dict]] = defaultdict(list)
_lock = asyncio.Lock()


def _get_client():
    global _client
    if _client is None:
        from openviking_sdk import AsyncHTTPClient

        _client = AsyncHTTPClient(url=OV_URL, api_key=OV_API_KEY)
    return _client


async def healthy() -> bool:
    """60 秒缓存的健康检查：一次真实 find 调用。"""
    global _health
    ok, ts = _health
    if time.time() - ts < 60:
        return ok
    if not OV_API_KEY:
        _health = (False, time.time())
        return False
    try:
        client = _get_client()
        if hasattr(client, "initialize"):
            await client.initialize()
        await asyncio.wait_for(client.find(query="ping", target_uri="viking://~/memories/"), timeout=10)
        _health = (True, time.time())
    except Exception:
        log.exception("openviking unhealthy")
        _health = (False, time.time())
    return _health[0]


async def recall(query: str) -> list[str]:
    """语义检索长期记忆，返回 abstract 列表。失败返回 []。"""
    if not await healthy():
        return []
    try:
        client = _get_client()
        res = await asyncio.wait_for(
            client.find(query=query, target_uri="viking://~/memories/", limit=RECALL_TOP_K),
            timeout=15,
        )
        items = res.get("memories", []) if isinstance(res, dict) else []
        out = [m.get("abstract", "") for m in items if m.get("abstract")]
        if out:
            log.info("ov recall %d: %s", len(out), [a[:30] for a in out])
        return out
    except Exception:
        log.exception("ov recall failed")
        return []


async def record_turn(user_id: int, user_text: str, reply: str) -> None:
    """缓冲对话，每 COMMIT_EVERY 轮向 OpenViking 提交一次会话提取记忆。"""
    if not OV_API_KEY:
        return
    buf = _pending[user_id]
    buf.append({"role": "user", "content": user_text})
    buf.append({"role": "assistant", "content": reply})
    if len(buf) < COMMIT_EVERY * 2:
        return
    async with _lock:
        if len(_pending[user_id]) < COMMIT_EVERY * 2:
            return
        batch, _pending[user_id] = _pending[user_id], []
    if not await healthy():
        _pending[user_id] = batch + _pending[user_id]  # 放回去下次再试
        return
    try:
        client = _get_client()
        info = await client.create_session(session_id=f"tg_{user_id}_{int(time.time())}")
        session = client.session(session_id=info["session_id"])
        for msg in batch:
            await session.add_message(role=msg["role"], content=msg["content"])
        result = await session.commit()
        log.info("ov committed %d msgs, task=%s", len(batch), result)
    except Exception:
        log.exception("ov commit failed")
        _pending[user_id] = batch + _pending[user_id]
