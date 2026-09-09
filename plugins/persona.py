"""persona 插件：人设加载（soul/*.md，委托 soul.py 库）+ tinynote 近况块。

_tinynote_block 逻辑自 bot.py 搬入，行为逐字一致：按修改时间取最新几篇小本本
内容，拼成注入聊天上下文的近况块。
"""

from pathlib import Path

import soul

BASE_DIR = Path(__file__).resolve().parent.parent
TINYNOTE_DIR = BASE_DIR / "tinynote"

requires: list[str] = []
provides = ["persona"]


class PersonaService:
    def system_prompt(self, memories_block: str = "") -> str:
        return soul.build_system(memories_block)

    def tinynote_block(self, max_chars: int = 800) -> str:
        """她小本本里最近写的内容（按修改时间取最新几篇），注入聊天上下文，让她能主动分享自己的新发现。"""
        try:
            files = sorted(
                (f for f in TINYNOTE_DIR.iterdir() if f.is_file()),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
        except Exception:
            return ""
        out: list[str] = []
        total = 0
        for f in files:
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not txt:
                continue
            chunk = txt[: max_chars - total]
            out.append(f"◆ {f.name}\n{chunk}")
            total += len(chunk)
            if total >= max_chars:
                break
        return "\n\n".join(out)


def apply(ctx) -> None:
    ctx.inject("config")
    ctx.provide("persona", PersonaService())
