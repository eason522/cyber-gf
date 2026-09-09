"""chat 插件：核心流水线（原 bot.py 的 chat_stream/_stream_once + TG/Discord 两份
process_message 的统一下沉）。

- stream(user_id, user_text, recall_task=None)：流式事件生成器，依次产出
  ("emotion", e) / ("voice", v) / ("sentence", s) / ("status", kind) / ("done", reply, emotion)。
  支持工具调用循环（最多 MAX_TOOL_ROUNDS 轮、末轮强制 reply）。时序保持原状：
  先起 recall task（与 LLM 首 token 并行）再判深度再流式。
- process(user_id, user_text, ui)：共享的"收事件流→TTS→发消息"派发，平台只提供
  ui 回调对象（协议见 process 文档字符串），消除 bot.py/discord_bot.py 的重复代码。
- 事件总线：process 入口 emit "message.received"，回复完成 emit "reply.done"。
"""

import asyncio
import json
import logging
import re
import time

from plugins.tts import EMOTIONS, SPEAKABLE

log = logging.getLogger("cyber-gf.chat")

requires = ["llm", "persona", "sessions", "memory", "tools", "depth", "tts", "asr"]
provides = ["chat"]

REPLY_TOOL = [{
    "type": "function",
    "function": {
        "name": "reply",
        "description": "以女朋友身份回复他。text 是 1~3 句口语台词，emotion 是这句话的主导情绪",
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": ["撒娇", "温柔", "开心", "难过", "生气", "害羞", "平静"],
                    "description": "这句话的主导情绪，先输出它",
                },
                "voice": {
                    "type": "string",
                    "description": "演绎方式（可选）：这句话要怎么念，写给语音合成的语气指令，"
                                   "如「带着哭腔」「兴奋地喊出来」「慵懒地拖着尾音」。"
                                   "绝大多数回复就是正常说话，直接省略此字段。"
                                   "「气声耳语」是极少数特殊时刻（讲秘密、深夜贴耳情话、害羞到不敢出声）"
                                   "才用的演绎，十句里最多一句，绝不连用",
                },
                "text": {"type": "string", "description": "口语台词：日常闲聊 1~3 句；系统提示走心时刻时，写 5~8 句的深情段落（至少100字），把心意说完整"},
            },
            "required": ["emotion", "text"],
        },
    },
}]

# 模型可调用工具时的系统提示补充
CAPABILITY_NOTE = (
    "\n\n（你有工具可以用：get_current_time 查真实时间（他问时间必须调用，不许自己猜）、"
    "web_search 联网搜索、list_directory / read_file / write_file 浏览和读写服务器上的文件。"
    "需要时先调工具，拿到结果后再调 reply 回复他；工具结果用你自己的话说，别照念。用不上工具就直接 reply。）"
)
MAX_TOOL_ROUNDS = 4

TEXT_KEY = re.compile(r'"text"\s*:\s*"')
EMOTION_RE = re.compile(r'"emotion"\s*:\s*"([^"]+)"')
VOICE_RE = re.compile(r'"voice"\s*:\s*"([^"]+)"')
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;~…\n])")


def _json_unescape(s: str) -> str:
    if s.endswith("\\"):
        s = s[:-1]
    return s.replace("\\\\", "\x00").replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\x00", "\\")


def _extract_stream_text(raw: str) -> str:
    """从流式累积的工具调用参数 JSON 中取出 text 字段的当前内容。"""
    m = TEXT_KEY.search(raw)
    if not m:
        return ""
    s = raw[m.end():]
    s = re.sub(r'(?<!\\)"\s*\}?\s*$', "", s)  # 去掉收尾引号
    return _json_unescape(s)


def _speakable(s: str) -> bool:
    """分句碎片里要有真实文字才值得合成——纯标点（如省略号"…"被独立切出）TTS 会返回空音频。"""
    return bool(s.strip()) and bool(SPEAKABLE.search(s))


class ChatService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.llm_model
        self._persona = ctx.inject("persona")
        self._sessions = ctx.inject("sessions")
        self._memory = ctx.inject("memory")
        self._tools = ctx.inject("tools")
        self._depth = ctx.inject("depth")
        self._tts = ctx.inject("tts")

    async def _stream_once(self, msgs: list, effort: str, force_reply: bool, result: dict):
        """单轮流式生成。产出 ("emotion", e) / ("voice", v) / ("sentence", s) 事件。

        模型调用行动工具（非 reply）时：不产出任何事件，把工具调用放进
        result["tool_calls"] 由外层执行后重开一轮；正常回复时把最终结果放进
        result["reply"] / result["emotion"]。
        """
        llm = self._ctx.inject("llm")  # 调用时注入，允许上层替换/fake
        stream = await llm.chat.completions.create(
            model=self._model,
            messages=msgs,
            tools=self._tools.defs() + REPLY_TOOL,
            tool_choice={"type": "function", "function": {"name": "reply"}} if force_reply else "auto",
            stream=True,
            extra_body=(
                {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
                if effort == "high"
                else {"thinking": {"type": "disabled"}}
            ),
        )
        slots: dict[int, dict] = {}  # 并行工具调用按 index 收集
        raw = ""  # reply 工具参数（引用 slots[0] 的累积值）
        plain = ""  # 模型没走工具调用时的兜底
        thinking = ""  # 思考过程（走心档位开启 thinking 时模型输出）
        emitted = 0  # 已产出的句段数
        emotion_seen: str | None = None
        voice_seen: str | None = None
        diverted = False  # 首个工具不是 reply → 本轮只为取工具结果，不产生事件
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                thinking += getattr(delta, "reasoning_content", None) or ""
                tc = getattr(delta, "tool_calls", None)
                if tc:
                    for c in tc:
                        slot = slots.setdefault(c.index or 0, {"id": "", "name": "", "args": ""})
                        if c.id:
                            slot["id"] = c.id
                        if getattr(c.function, "name", None):
                            slot["name"] = c.function.name
                        slot["args"] += c.function.arguments or ""
                    name0 = slots.get(0, {}).get("name", "")
                    if name0 and name0 != "reply" and not plain:
                        diverted = True
                    elif name0 == "reply":
                        raw = slots[0]["args"]
                elif delta.content:
                    plain += delta.content
                if diverted:
                    continue
                if raw and emotion_seen is None:
                    em = EMOTION_RE.search(raw)
                    if em:
                        emotion_seen = em.group(1)
                        yield ("emotion", emotion_seen)
                if raw and voice_seen is None:
                    vm = VOICE_RE.search(raw)
                    if vm:
                        voice_seen = vm.group(1)
                        yield ("voice", voice_seen)
                decoded = _extract_stream_text(raw) if raw else plain
                parts = SENT_SPLIT.split(decoded)
                complete = [p.strip() for p in parts[:-1] if _speakable(p)]
                while emitted < len(complete):
                    yield ("sentence", complete[emitted])
                    emitted += 1
        finally:
            try:
                await stream.close()
            except Exception:
                pass

        if diverted:
            if thinking:
                log.info("thinking(tool round): %s", thinking[:500])
            calls = []
            for i, s in sorted(slots.items()):
                try:
                    args = json.loads(s["args"]) if s["args"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append({"id": s["id"] or f"call_{i}", "name": s["name"], "args": args})
            result["tool_calls"] = calls
            return

        if raw:
            args = json.loads(raw)
            reply = (args.get("text") or "").strip()
            emotion = args.get("emotion") or emotion_seen or "平静"
        else:
            reply = plain.strip()
            emotion = emotion_seen or "平静"
        if not reply:
            raise ValueError("empty reply")
        if emotion not in EMOTIONS:
            emotion = "平静"
        # 冲刷剩余文本
        decoded = _extract_stream_text(raw) if raw else plain
        rest = [p.strip() for p in SENT_SPLIT.split(decoded) if _speakable(p)]
        while emitted < len(rest):
            yield ("sentence", rest[emitted])
            emitted += 1
        result["reply"] = reply
        result["emotion"] = emotion
        if thinking:
            log.info("thinking: %s", thinking[:1000])

    async def stream(self, user_id: int, user_text: str, recall_task: asyncio.Task | None = None):
        """流式回复生成器：依次产出 ("emotion", e) / ("sentence", s)，最后 ("done", reply, emotion)。

        支持工具调用循环：模型调行动工具 → 执行 → 结果回填 → 重新生成，直到给出 reply。
        recall_task：语音场景下平台层与 ASR 并行的预检索任务（memory.recall 的 task）。
        """
        sessions = self._sessions
        store = sessions.get_store(user_id)
        judge_task = asyncio.create_task(self._depth.judge(user_text))
        is_pre_recall = recall_task is not None
        if recall_task is None:
            recall_task = asyncio.create_task(self._memory.recall(user_text))
        yield ("status", "recall")  # 平台层可借此显示"正在回忆"
        effort, recalled = await asyncio.gather(judge_task, recall_task)
        if not recalled and is_pre_recall:
            recalled = await self._memory.recall(user_text)  # 预检索为空，用真实文本补一次
        yield ("status", "")
        if recalled:
            mem_block = recalled
        elif store["memories"]:  # OpenViking 不可用时回退到本地记忆
            mem_block = "\n".join(f"- {m}" for m in store["memories"])
        else:
            mem_block = ""
        system = self._persona.system_prompt(mem_block) + CAPABILITY_NOTE
        notes = self._persona.tinynote_block()
        if notes:
            system += "\n\n# 她的小本本近况（她自己写的日记/冲浪笔记，聊天时可以自然地分享里面的新发现）\n\n" + notes
        if effort == "high":
            system += (
                "\n\n（走心時刻：他這句話觸動了你心底最軟的地方。現在拋開平常發短訊息的習慣，"
                "像寫一封短信、一段獨白那樣，把你的心意完整說出來——認真地說 5~8 句、至少 100 字，"
                "回憶你們之間的細節，說你平時不好意思說的話。慢慢說，他會聽完的。"
                "感覺大概是這樣的節奏（只是示範語氣和長度，絕對不要照抄內容，說你自己心裡的話）：「寶貝，你知道嗎……其實我有好多話一直想跟你說……（以下省略）」）"
            )
        log.info("depth=%s", effort)
        msgs = [{"role": "system", "content": system}]
        msgs.extend(sessions.recent(user_id))
        msgs.append({"role": "user", "content": user_text})

        result: dict = {}
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            result = {}
            async for ev in self._stream_once(msgs, effort, round_no == MAX_TOOL_ROUNDS, result):
                yield ev
            tool_calls = result.get("tool_calls")
            if not tool_calls:
                break
            log.info("tool round %d: %s", round_no, [(t["name"], t["args"]) for t in tool_calls])
            if any(t["name"] == "web_search" for t in tool_calls):
                yield ("status", "surf")  # 平台层可借此显示"正在刷小红书"
            msgs.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": t["id"], "type": "function",
                    "function": {"name": t["name"], "arguments": json.dumps(t["args"], ensure_ascii=False)},
                } for t in tool_calls],
            })
            for t in tool_calls:
                out = "（先别急着回复，把工具结果用上再说）" if t["name"] == "reply" else await self._tools.run(t["name"], t["args"])
                msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
            yield ("status", "")
        reply = result["reply"]
        emotion = result["emotion"]
        log.info("reply (%s): %s", emotion, reply[:150])

        sessions.append_turn(user_id, user_text, reply, time.time())
        self._ctx.create_task(self._memory.record_turn(user_id, user_text, reply))
        yield ("done", reply, emotion)

    async def _keepalive(self, ui, stop: asyncio.Event) -> None:
        """生成期间的 keepalive：按原 Telegram 节奏（4.5s）让平台显示"正在录音/输入"。"""
        while not stop.is_set():
            try:
                await ui.pulse("record")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                pass

    async def _set_status(self, ui, kind: str | None) -> None:
        try:
            await ui.status(kind)
        except Exception:
            pass

    async def process(self, user_id: int, user_text: str, ui,
                      recall_task: asyncio.Task | None = None) -> None:
        """共享的消息处理流水线（原 bot.py / discord_bot.py 两份 process_message 的合并）。

        语音优先：回复不超过 WHOLE_TTS_MAX 字（或悄悄话场景）攒整段一次合成，
        保住省略号等情绪细节且语气一致；超长回复才按句并行合成抢速度。语音按序先发，文字最后到。
        recall_task：语音场景下平台层与 ASR 并行的预检索任务，透传给 stream()。

        ui 是平台提供的回调对象，协议：
        - async send_text(text: str)：发文字（完整回复 / 出错提示）
        - async send_voice(ogg_path)：发语音文件（Path）；返回后由 chat 负责删除临时文件
        - async status(kind: str | None)："recall"=正在回忆 / "surf"=正在刷小红书 / None=清除
          （Telegram 没有自定义状态，实现为 no-op 即可）
        - async pulse(kind: str)：keepalive 动作提示，kind="record"（TG 录音中 / Discord typing）
        """
        await self._ctx.emit("message.received", user_id=user_id, text=user_text)
        stop = asyncio.Event()
        keepalive = asyncio.create_task(self._keepalive(ui, stop))
        emotion = "平静"
        voice_hint = ""
        whisper = False
        pending: list[str] = []  # 未派发的句子缓冲（攒整段用）
        split = False            # 累计超阈值后转逐句并行合成
        tasks: list[asyncio.Task] = []
        full_reply = ""
        t0 = time.time()
        try:
            async for ev in self.stream(user_id, user_text, recall_task=recall_task):
                if ev[0] == "emotion":
                    emotion = ev[1]
                elif ev[0] == "voice":
                    voice_hint = ev[1]
                    whisper = self._tts.is_whisper(voice_hint)
                elif ev[0] == "sentence":
                    if whisper:
                        continue  # 悄悄话攒整段（分句合成气声会逐句漂移）
                    if split:
                        tasks.append(asyncio.create_task(self._tts.safe_ogg(ev[1], emotion, voice_hint)))
                    else:
                        pending.append(ev[1])
                        if sum(map(len, pending)) > self._tts.WHOLE_TTS_MAX:
                            split = True
                            for s in pending:
                                tasks.append(asyncio.create_task(self._tts.safe_ogg(s, emotion, voice_hint)))
                            pending.clear()
                elif ev[0] == "status":
                    await self._set_status(ui, ev[1] or None)
                else:
                    _, full_reply, emotion = ev
        except Exception:
            log.exception("llm failed")
            stop.set()
            keepalive.cancel()
            await self._set_status(ui, None)
            await ui.send_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
            return
        await self._set_status(ui, None)
        if not split and full_reply:
            tasks.append(asyncio.create_task(self._tts.safe_ogg(full_reply, emotion, voice_hint)))
        log.info("llm stream done in %.1fs, %d sentences, emotion=%s voice=%s split=%s", time.time() - t0, len(tasks), emotion, voice_hint or "-", split)

        oggs = await asyncio.gather(*tasks)
        stop.set()
        keepalive.cancel()
        for ogg in oggs:
            if not ogg:
                continue
            try:
                await ui.pulse("record")
            except Exception:
                pass
            await ui.send_voice(ogg)
            ogg.unlink(missing_ok=True)
        await ui.send_text(full_reply)  # 文字最后到
        await self._ctx.emit("reply.done", user_id=user_id)


def apply(ctx) -> None:
    ctx.provide("chat", ChatService(ctx))
