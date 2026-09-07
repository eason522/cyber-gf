"""OpenViking 长期记忆封装：语义检索（find）+ 会话提交（session commit）。

所有函数在 OpenViking 不可用时优雅降级（返回空/跳过），绝不阻塞主链路。
事实类记忆带 peer_id 写入 viking://~/peers/boyfriend/memories/，
检索时同时搜 self 与 peer 两个空间。
"""

import asyncio
import logging
import os
import time
from collections import defaultdict

log = logging.getLogger("cyber-gf.ov")

OV_URL = os.getenv("OV_URL", "http://127.0.0.1:1933")
OV_API_KEY = os.getenv("OV_API_KEY", "")
OV_PEER_ID = os.getenv("OV_PEER_ID", "boyfriend")
COMMIT_EVERY = int(os.getenv("MEMORY_EVERY", "4"))
RECALL_TOP_K = int(os.getenv("OV_RECALL_TOP_K", "5"))

_client = None
_health: tuple[bool, float] = (False, 0.0)
_pending: dict[int, list[dict]] = defaultdict(list)
_lock = asyncio.Lock()


def _get_client():
    global _client
    if _client is None:
        from openviking_sdk import SyncHTTPClient

        _client = SyncHTTPClient(url=OV_URL, api_key=OV_API_KEY)
        _client.initialize()
    return _client


async def healthy() -> bool:
    """60 秒缓存的健康检查：廉价的 /health HTTP 请求。"""
    global _health
    ok, ts = _health
    if time.time() - ts < 60:
        return ok
    if not OV_API_KEY:
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            r = await c.get(f"{OV_URL}/health")
            r.raise_for_status()
        _health = (True, time.time())
    except Exception:
        log.exception("openviking unhealthy")
        _health = (False, time.time())
    return _health[0]


async def recall(query: str) -> list[str]:
    """语义检索长期记忆：先查 peer 空间，结果太少再补查 self。失败返回 []。"""
    if not await healthy():
        return []
    try:
        client = _get_client()

        def _find(uri):
            return client.find(query=query, target_uri=uri, limit=RECALL_TOP_K)

        res = await asyncio.wait_for(
            asyncio.to_thread(_find, f"viking://~/peers/{OV_PEER_ID}/memories/"), timeout=15
        )
        items = res.get("memories", []) if isinstance(res, dict) else []
        if len(items) < 2:
            res2 = await asyncio.wait_for(
                asyncio.to_thread(_find, "viking://~/memories/"), timeout=15
            )
            if isinstance(res2, dict):
                items += res2.get("memories", [])
        items.sort(key=lambda m: m.get("score", 0), reverse=True)
        out = [m["abstract"] for m in items[:RECALL_TOP_K] if m.get("abstract")]
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

    def _commit():
        client = _get_client()
        info = client.create_session()
        session = client.session(session_id=info["session_id"])
        for msg in batch:
            session.add_message(role=msg["role"], content=msg["content"], peer_id=OV_PEER_ID)
        return session.commit()

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_commit), timeout=60)
        log.info("ov committed %d msgs, task=%s", len(batch), result.get("task_id"))
    except Exception:
        log.exception("ov commit failed")
        _pending[user_id] = batch + _pending[user_id]
