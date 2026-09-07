from pathlib import Path

SOUL_DIR = Path(__file__).parent / "soul"


def load(name: str) -> str:
    return (SOUL_DIR / name).read_text(encoding="utf-8").strip()


def build_system(memories_block: str = "") -> str:
    parts = [load("IDENTITY.md"), load("SOUL.md"), load("USER.md")]
    if memories_block:
        parts.append("# 此刻你想起的关于他的事\n\n" + memories_block)
    return "\n\n---\n\n".join(parts)
