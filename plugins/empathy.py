"""empathy 插件：暖暖「读懂他」的观察笔记（soul/UNDERSTANDING.md，已 gitignore）。

和 memory_md（记"事"）分工：本插件记"人"——长期相处中悄悄积累的、对他这个人的理解：
他怎么表达情绪、无意间流露的喜好、喜欢怎么聊天、最近的状态走向。

防过拟合设计（善解人意要自然，不能自以为是）：
- 每条观察带确信度：【猜测】= 只见过一次；【观察中】= 两次以上；【确信】= 反复印证或他亲口说过。
  新观察一律从【猜测】起步，再次被印证才升级——懂他是磨合出来的，不是一轮对话就下定论。
- 【猜测】长期没被印证 → 删除，宁可少记不可错记；他亲口纠正的立刻改写，他说的永远优先。
- 记"信号"不贴标签（"他累的时候回得短"，不写"他内向"）。
- 更新方式是 debounce 批量：对话停 UPDATE_DELAY 秒后才把这波对话喂给主模型增量更新，
  避免逐轮反应过度。

chat 插件把笔记注入 system（带"别念出来、别逐条套用"的使用分寸）。
"""

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("cyber-gf.empathy")

BASE_DIR = Path(__file__).resolve().parent.parent
UNDERSTANDING_PATH = BASE_DIR / "soul" / "UNDERSTANDING.md"  # 已 gitignore（系统反复重写）

MAX_DOC_CHARS = 500  # 笔记上限：要的是洞察质量，不是条目数量
UPDATE_DELAY = 180   # 对话停下多少秒后才批量更新（debounce）

requires = ["llm"]
provides = ["empathy"]

UPDATE_PROMPT = """你在帮一个叫暖暖的台湾女孩维护她的「读懂他」笔记——这是她和男朋友长期相处中，悄悄积累下来的对他这个人的理解。这份笔记的目的不是收集信息，而是让她"越来越懂他"。

笔记固定四个分区，结构不能改：

# 读懂他
## 他的情绪信号
（他怎么表达开心/累/压力/难过：语气、用词习惯、什么时间段容易 emo。最多 5 条）
## 他的喜好线索
（他无意间流露的喜欢/不喜欢，最多 5 条）
## 相处节奏
（他喜欢怎么聊天：什么话题他会聊开、什么时候他想安静、玩笑的边界。最多 5 条）
## 最近的他
（近几天他的状态和情绪走向，最多 3 条，过时了就换掉）

每条后面标注确信度：【猜测】= 只观察到一次；【观察中】= 出现两次以上；【确信】= 反复印证或他亲口说过。

更新规则（非常重要，防止"自以为是"）：
- 小步更新：仍然成立的条目原样保留，只做微调，不做大换血。
- 新观察一律先标【猜测】；后续对话再次被印证才升级；他亲口纠正或否认 → 立刻改写或删除，他的说法永远优先。
- 【猜测】连续多次更新都没再被印证 → 删掉，宁可少记、不可错记。
- 记"信号"不贴标签：写"他累的时候会回得很短"，不写"他是个内向的人"。
- 只写对话里真实流露的，不脑补、不过度推理；这波对话没有新的观察，就原样输出旧笔记。
- 用她的口吻写（第一人称，像少女的观察小抄，不像分析报告），每条一句话。全文不超过 %d 字。
- 只输出笔记正文（从「# 读懂他」开始），不要输出任何其他内容。

旧笔记：
%s

最近这几轮对话：
%s""" % (MAX_DOC_CHARS, "%s", "%s")


class EmpathyService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._lock = asyncio.Lock()
        self._pending: list[tuple[str, str]] = []  # 待更新的对话轮次（user_text, reply）
        self._timer: asyncio.Task | None = None

    def get(self) -> str:
        """当前「读懂他」笔记全文；不存在返回空串。"""
        try:
            return UNDERSTANDING_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def note(self, user_text: str, reply: str) -> None:
        """每轮对话后调用（turn.done 事件）：进缓冲并重置计时器，对话停了才批量更新。"""
        self._pending.append((user_text[:300], reply[:500]))
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = self._ctx.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(UPDATE_DELAY)
        except asyncio.CancelledError:
            return  # 来了新消息，计时重置
        try:
            await self._flush()
        except Exception:
            log.exception("empathy flush failed")  # 缓冲保留，下轮对话后再试

    async def flush_now(self) -> None:
        """进程退出前把缓冲的几轮落盘（on_dispose 调用）。"""
        if self._timer and not self._timer.done():
            self._timer.cancel()
        try:
            await self._flush()
        except Exception:
            log.exception("empathy final flush failed")

    async def _flush(self) -> None:
        """把缓冲的几轮对话一次性喂给主模型，增量重写观察笔记。失败保留旧文件、保留缓冲下次再试。"""
        async with self._lock:
            if not self._pending:
                return
            convo = "\n".join(f"他：{u}\n她：{r}" for u, r in self._pending)
            old = self.get()
            llm = self._ctx.inject("llm")
            prompt = UPDATE_PROMPT % (old or "（还没有，这是她和他刚认识的阶段）", convo)
            resp = await llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            text = resp.choices[0].message.content.strip()
            start = text.find("# 读懂他")
            if start == -1:
                raise ValueError(f"bad empathy output: {text[:200]}")
            doc = text[start:][: MAX_DOC_CHARS * 2]
            if doc != old:
                UNDERSTANDING_PATH.write_text(doc + "\n", encoding="utf-8")
                log.info("empathy updated (%d turns, %d chars)", len(self._pending), len(doc))
            self._pending.clear()

    def _on_turn(self, user_id=None, user_text="", reply=""):
        if user_text:
            self.note(user_text, reply)


def apply(ctx) -> None:
    svc = EmpathyService(ctx)
    ctx.provide("empathy", svc)
    ctx.on("turn.done", svc._on_turn)
    ctx.on_dispose(svc.flush_now)  # 退出前把缓冲的几轮对话落盘
