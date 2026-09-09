"""interests 插件：暖暖的兴趣画像（data/interests.md），系统定期用主模型维护。

素材来自她的长期记忆（data/*.json 的 memories）、小本本（tinynote/）和近期对话，
综合提炼成一份"兴趣手账"。防过拟合靠固定四分区结构 + 更新规则（见 REFRESH_PROMPT）：
长期热爱不轻易降级、最近上头限量轮换、"想探索的新领域"永远留空位——避免兴趣越刷越窄。

画像注入聊天 system（chat 插件可选消费），surf 插件用它引导冲浪方向。
"""

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("cyber-gf.interests")

BASE_DIR = Path(__file__).resolve().parent.parent
INTERESTS_PATH = BASE_DIR / "data" / "interests.md"
TINYNOTE_DIR = BASE_DIR / "tinynote"
DATA_DIR = BASE_DIR / "data"

MAX_MATERIAL_CHARS = 3000   # 喂给模型的素材总量上限
MAX_DOC_CHARS = 800         # 手账全文上限（防膨胀）

requires = ["llm"]
provides = ["interests"]

REFRESH_PROMPT = """你在帮一个叫暖暖的台湾女孩维护她的「兴趣手账」。根据她的旧手账、长期记忆、小本本笔记和近期对话，更新这份手账。

手账固定四个分区，结构不能改：

# 暖暖的兴趣手账
## 长期热爱
（她稳定喜欢的东西，最多 5 条）
## 最近上头
（最近新迷上的，标注大概什么时候入坑，最多 4 条）
## 冷却中
（以前喜欢但最近少提的，留作回顾，最多 3 条）
## 想探索的新领域
（她还没接触过、但可能感兴趣的方向，1~3 个）

更新规则（很重要，防止兴趣越刷越窄）：
- 小步更新：优先微调旧手账，仍然成立的原句保留，不做大换血。
- 「长期热爱」只有连续多次更新都明显冷淡，才降级到「冷却中」；绝不因为一次没提到就删除。
- 「最近上头」有新的进来时，最旧的一条降级到「冷却中」或移除。
- 「想探索的新领域」必须始终至少保留 1 个，且不能和现有兴趣同主题——这是她保持好奇心的窗口。可以从对话里她随口好奇过但没深究的事里找灵感。
- 只提炼"她"的兴趣，不要把他的喜好当成她的。
- 用她的口吻写（第一人称、轻松随意，像手账不像报告），每条一句话，写清楚喜欢它的点在哪。全文不超过 %d 字。
- 只输出手账正文（从「# 暖暖的兴趣手账」开始），不要输出任何其他内容。

旧手账：
%s

她的长期记忆：
%s

她的小本本近况：
%s

近期对话片段：
%s""" % (MAX_DOC_CHARS, "%s", "%s", "%s", "%s")


def _collect_material() -> tuple[str, str, str]:
    """收集素材：长期记忆 + 小本本近况 + 近期对话片段（各自截断，总量受控）。"""
    memories: list[str] = []
    history_lines: list[str] = []
    for f in sorted(DATA_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.stem in ("contact",) or not f.stem.isdigit():
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for m in data.get("memories", []):
            if m not in memories:
                memories.append(m)
        for msg in data.get("history", [])[-10:]:
            who = "他" if msg.get("role") == "user" else "她"
            history_lines.append(f"{who}：{msg.get('content', '')[:100]}")
        if len(memories) >= 40:
            break
    notes: list[str] = []
    try:
        files = sorted(
            (f for f in TINYNOTE_DIR.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )[:3]
        for f in files:
            txt = f.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                notes.append(f"◆ {f.name}\n{txt[:800]}")
    except Exception:
        pass
    mem_s = "\n".join(f"- {m}" for m in memories)[:MAX_MATERIAL_CHARS] or "（空）"
    notes_s = "\n\n".join(notes)[:MAX_MATERIAL_CHARS] or "（空）"
    hist_s = "\n".join(history_lines[-30:])[:MAX_MATERIAL_CHARS] or "（空）"
    return mem_s, notes_s, hist_s


class InterestsService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._hours = cfg.interests_hours

    def get(self) -> str:
        """当前兴趣画像全文；不存在返回空串。"""
        try:
            return INTERESTS_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    async def refresh(self) -> None:
        """调主模型综合素材重写手账。失败保留旧文件。"""
        old = self.get()
        mem_s, notes_s, hist_s = _collect_material()
        llm = self._ctx.inject("llm")
        prompt = REFRESH_PROMPT % (old or "（还没有，这是她第一次写手账）", mem_s, notes_s, hist_s)
        resp = await llm.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        start = text.find("# 暖暖的兴趣手账")
        if start == -1:
            raise ValueError(f"bad interests output: {text[:200]}")
        INTERESTS_PATH.write_text(text[start:][: MAX_DOC_CHARS * 2] + "\n", encoding="utf-8")
        log.info("interests refreshed (%d chars)", len(text) - start)

    async def loop(self) -> None:
        """定期刷新：启动 5 分钟后先跑一次（手账不存在或过期才跑），之后每 INTERESTS_HOURS 一次。"""
        await asyncio.sleep(300)
        while True:
            try:
                stale = True
                try:
                    stale = time.time() - INTERESTS_PATH.stat().st_mtime > self._hours * 3600
                except FileNotFoundError:
                    pass
                if stale:
                    await self.refresh()
            except Exception:
                log.exception("interests refresh error")
            await asyncio.sleep(self._hours * 3600)


def apply(ctx) -> None:
    svc = InterestsService(ctx)
    ctx.provide("interests", svc)
    ctx.create_task(svc.loop())
