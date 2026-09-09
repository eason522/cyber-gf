"""memory_md 插件：暖暖的随身记忆（soul/MEMORY.md），分层记忆里的"热层"。

分层设计：
- soul/MEMORY.md（本插件，实时维护）：简单的、高频的、特别重要的记忆——
  他是谁、重要约定与嘱托、最近的事。每条消息都注入 system，随时可用。
- OpenViking（memory_openviking 插件）：复杂的、信息量大的、低频远期的记忆，
  需要时按语义检索调取。
- data/<uid>.json 的本地 memories：OV 挂掉时的兜底。

主动记忆（他明确说"记住……"）由 UPDATE_PROMPT 规则保证优先进「重要约定与嘱托」；
被动记忆是每轮对话后 note() 的自动增量更新（fire-and-forget，锁串行防并发写坏文件）。
"""

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("cyber-gf.memory_md")

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_MD_PATH = BASE_DIR / "soul" / "MEMORY.md"  # 已 gitignore（实时重写，不进仓库）

MAX_DOC_CHARS = 500  # 随身记忆上限：要的是高频/重要，不是全集（全集在 OpenViking）

requires = ["llm"]
provides = ["memory_md"]

UPDATE_PROMPT = """你在帮一个叫暖暖的台湾女孩维护她的「随身记忆」——这是她每条消息都会带在身上的一小张记忆卡，不是大记忆库（低频、远期、复杂的细节有大记忆库负责，别往这里堆）。

随身记忆固定三个分区，结构不能改：

# 随身记忆
## 他是谁
（关于他的稳定事实：职业/身份、称呼、喜好厌恶、生活习惯等，最多 8 条）
## 重要约定与嘱托
（他明确要求她记住的事、两人的约定、重要日期，必须长期保留，绝不能丢）
## 最近的事
（近几天他的状态、正在发生的事、高频话题，最多 5 条，过时了就换掉）

更新规则：
- 小步更新：根据最新这轮对话增量调整，仍然成立的条目原样保留。
- 他明确说"记住/别忘了"的事 → 必须进「重要约定与嘱托」。
- 从这轮对话能稳定推断出的事实也记（比如他提到规培经历 → 他是医学生/医生），但只写把握大的。
- 只记值得长期/高频用的；闲聊废话、一次性话题不进。
- 每条一句简短陈述，全文不超过 %d 字。
- 没什么值得更新的，就原样输出旧记忆。
- 只输出记忆卡正文（从「# 随身记忆」开始），不要输出任何其他内容。

旧随身记忆：
%s

最新这轮对话：
他：%s
她：%s""" % (MAX_DOC_CHARS, "%s", "%s", "%s")


class MemoryMdService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._lock = asyncio.Lock()

    def get(self) -> str:
        """当前随身记忆全文；不存在返回空串。"""
        try:
            return MEMORY_MD_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    async def note(self, user_id: int, user_text: str, reply: str) -> None:
        """每轮对话后增量更新记忆卡（锁串行，失败保留旧文件）。"""
        async with self._lock:
            old = self.get()
            llm = self._ctx.inject("llm")
            prompt = UPDATE_PROMPT % (old or "（还没有，这是第一条）",
                                      user_text[:300], reply[:500])
            resp = await llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            text = resp.choices[0].message.content.strip()
            start = text.find("# 随身记忆")
            if start == -1:
                raise ValueError(f"bad memory_md output: {text[:200]}")
            doc = text[start:][: MAX_DOC_CHARS * 2]
            if doc != old:
                MEMORY_MD_PATH.write_text(doc + "\n", encoding="utf-8")
                log.info("memory_md updated (%d chars)", len(doc))


def apply(ctx) -> None:
    ctx.provide("memory_md", MemoryMdService(ctx))
