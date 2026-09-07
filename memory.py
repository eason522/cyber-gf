import json
import logging
from pathlib import Path

log = logging.getLogger("cyber-gf.memory")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

MAX_MEMORIES = 40

EXTRACT_PROMPT = """你在帮一个AI女友维护她关于男朋友的长期记忆。根据已有记忆和最近的对话，更新记忆列表。

规则：
- 只记值得长期记住的事：他的名字/称呼、喜好、厌恶、生活习惯、工作、重要事件和日期、他说过的心情、两人的约定、他的口头禅等。
- 不记闲聊废话、客套话、一次性的话题。
- 已有记忆如果仍然有效就保留；如果有新信息就更新它；如果被明确否定就删除。
- 每条记忆写成一句简短的第三人称陈述，例如「他在互联网公司上班，经常加班」。
- 最多保留 %d 条，超出时优先保留更重要、更近期的。
- 只输出一个 JSON 数组（字符串数组），不要输出任何其他内容。

已有记忆：
%s

最近对话：
%s""" % (MAX_MEMORIES, "%s", "%s")


def _path(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"


def load(user_id: int) -> dict:
    try:
        data = json.loads(_path(user_id).read_text())
        return {"history": data.get("history", []), "memories": data.get("memories", [])}
    except Exception:
        return {"history": [], "memories": []}


def save(user_id: int, store: dict) -> None:
    _path(user_id).write_text(json.dumps(store, ensure_ascii=False, indent=1))


async def extract(llm, model: str, memories: list[str], recent: list[dict]) -> list[str]:
    """让 LLM 根据最近对话更新长期记忆。失败时返回原记忆。"""
    convo = "\n".join(
        ("他" if m["role"] == "user" else "她") + "：" + m["content"] for m in recent
    )
    prompt = EXTRACT_PROMPT % (
        json.dumps(memories, ensure_ascii=False) if memories else "（空）",
        convo or "（空）",
    )
    resp = await llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = resp.choices[0].message.content.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError(f"bad memory extract output: {text[:200]}")
    result = json.loads(text[start : end + 1])
    if not isinstance(result, list):
        raise ValueError("memory extract output is not a list")
    return [str(x) for x in result][:MAX_MEMORIES]
