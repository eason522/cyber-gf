"""sessions 插件：会话内存态（stores）、历史持久化（委托 memory.py 的 load/save）、
contact.json 读写、HISTORY_TURNS 截断。逻辑自 bot.py 搬入，行为一致。"""

import json
import time
from pathlib import Path

import memory as mem

BASE_DIR = Path(__file__).resolve().parent.parent
CONTACT_FILE = BASE_DIR / "data" / "contact.json"

requires: list[str] = []
provides = ["sessions"]


class SessionService:
    def __init__(self, history_turns: int):
        self.stores: dict[int, dict] = {}
        self.history_turns = history_turns

    def get_store(self, user_id: int) -> dict:
        if user_id not in self.stores:
            self.stores[user_id] = mem.load(user_id)
        return self.stores[user_id]

    def save_store(self, user_id: int) -> None:
        if user_id in self.stores:
            mem.save(user_id, self.stores[user_id])

    def recent(self, user_id: int) -> list[dict]:
        """拼装消息用的历史窗口：最近 HISTORY_TURNS*2 条。"""
        return self.get_store(user_id)["history"][-self.history_turns * 2 :]

    def append_turn(self, user_id: int, user_text: str, reply: str, ts: float) -> None:
        """记录一轮对话并截断到 HISTORY_TURNS*4 条（原 bot.py chat_stream 末尾逻辑）。"""
        store = self.get_store(user_id)
        store["history"].append({"role": "user", "content": user_text, "ts": ts})
        store["history"].append({"role": "assistant", "content": reply, "ts": ts})
        store["history"] = store["history"][-self.history_turns * 4 :]
        mem.save(user_id, store)

    def save_contact(self, platform: str, user_id: int, chat_id: int) -> None:
        CONTACT_FILE.parent.mkdir(exist_ok=True)
        CONTACT_FILE.write_text(json.dumps({
            "user_id": user_id, "chat_id": chat_id, "platform": platform, "ts": time.time(),
        }))

    def load_contact(self) -> dict:
        try:
            return json.loads(CONTACT_FILE.read_text())
        except Exception:
            return {}


def apply(ctx) -> None:
    cfg = ctx.inject("config")
    ctx.provide("sessions", SessionService(cfg.history_turns))
