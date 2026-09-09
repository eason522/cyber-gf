import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

import soul
import memory as mem
import ov_memory
import gf_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cyber-gf")

BASE_DIR = Path(__file__).parent
FFMPEG = str(BASE_DIR / "ffmpeg")

BOT_PLATFORM = os.getenv("BOT_PLATFORM", "telegram")  # telegram | discord
TG_TOKEN = os.getenv("TG_TOKEN", "")  # discord 模式下可缺省
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
TTS_VOICE = os.getenv("TTS_VOICE", "zh-TW-HsiaoChenNeural")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "20"))

llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
stores: dict[int, dict] = {}
MEMORY_EVERY = int(os.getenv("MEMORY_EVERY", "4"))
_extracting: set[int] = set()

# 心跳：主动关心
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "45"))
HEARTBEAT_SILENCE_H = float(os.getenv("HEARTBEAT_SILENCE_H", "2"))
ACTIVE_HOURS = (8, 23)  # 深夜免打扰（北京时间）
CONTACT_FILE = Path(__file__).parent / "data" / "contact.json"
# 心跳时她可以写小本本（给时间工具好让日记日期写对；不给联网搜索，保持安静场景纯粹）
HEARTBEAT_TOOLS = [t for t in gf_tools.TOOL_DEFS
                   if t["function"]["name"] in ("get_current_time", "list_directory", "read_file", "write_file")]


def _load_contact() -> dict:
    try:
        return json.loads(CONTACT_FILE.read_text())
    except Exception:
        return {}


def _save_contact(user_id: int, chat_id: int, platform: str = "telegram") -> None:
    CONTACT_FILE.parent.mkdir(exist_ok=True)
    CONTACT_FILE.write_text(json.dumps({
        "user_id": user_id, "chat_id": chat_id, "platform": platform, "ts": time.time(),
    }))

_whisper = None


def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        log.info("loading whisper model %s ...", WHISPER_MODEL)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def get_store(user_id: int) -> dict:
    if user_id not in stores:
        stores[user_id] = mem.load(user_id)
    return stores[user_id]


async def maybe_extract(user_id: int) -> None:
    store = get_store(user_id)
    hist = store["history"]
    if len(hist) < MEMORY_EVERY * 2 or len(hist) % (MEMORY_EVERY * 2) != 0:
        return
    if user_id in _extracting:
        return
    _extracting.add(user_id)
    try:
        store["memories"] = await mem.extract(
            llm, LLM_MODEL, store["memories"], hist[-MEMORY_EVERY * 2 :]
        )
        mem.save(user_id, store)
        log.info("memories updated for %s: %d items", user_id, len(store["memories"]))
    except Exception:
        log.exception("memory extract failed")
    finally:
        _extracting.discard(user_id)


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

# 情绪 → seed-tts-2.0 语音指令（additions.context_texts，官方字段，不计费不朗读）。
# 指令必须是纯"声音描写"（用……的语气说），对话式互动指令实测失效；
# 生气这条是官网对照实验验证过的措辞。外部数值参数（pitch/speech_rate/loudness）
# 会干扰模型自己的演绎，全部弃用
EMOTIONS = {
    "撒娇": "用撒娇、软软糯糯、甜腻的语气说",
    "温柔": "用温柔、轻声、宠溺的语气说",
    "开心": "用开心、轻快、雀跃的语气说",
    "难过": "用难过、委屈、带着点哭腔的语气说",
    "生气": "用非常生气、像在吵架一样凶巴巴的语气说",
    "害羞": "用害羞、犹豫、轻声细语的语气说",
    "平静": "",
}

# 悄悄话/耳语/气声类演绎：实测小和音色情绪表现力弱于 vv，故命中 voice 提示关键词时
# 切 vv 音色 + 官网对照验证过的纯气声指令；且悄悄话必须整段一次合成（分句并行合成
# 气声会逐句漂移，context_texts 混入 quote/前句/section_id 也会稀释指令，隔离实验实测），
# 由 process_message 攒句到流式结束后单段合成。其余情绪都用默认音色小和（自带台湾腔）
WHISPER_KEYWORDS = ("耳语", "悄悄话", "气声", "asmr")
WHISPER_VOICE = "zh_female_vv_uranus_bigtts"
WHISPER_INSTRUCTION = ("全程用纯气声耳语：声带完全不震动、没有一点真声和音调起伏，"
                       "只有气流摩擦的沙沙声，放慢语速、贴着耳朵轻轻地说")


def is_whisper(voice_hint: str) -> bool:
    return any(k in voice_hint.lower() for k in WHISPER_KEYWORDS)


def tts_params_for(emotion: str, voice_hint: str = "") -> dict:
    """语音指令走 additions.context_texts，且只放一条纯指令——混入引用上文/多条指令
    叠加都会稀释效果（隔离实验实测）。用户原话绝不进 context（同样会干扰）。"""
    hint = voice_hint.strip()
    if is_whisper(hint):
        return {"context": [WHISPER_INSTRUCTION], "voice": WHISPER_VOICE}
    instruction = "；".join(c for c in (EMOTIONS.get(emotion, ""), hint) if c)
    return {"context": [instruction] if instruction else []}

# 深度路由：明显日常的短消息走快速通道，拿不准的问裁判模型
DEEP_KEYWORDS = ("爱", "想你", "思念", "难过", "伤心", "哭", "emo", "分手", "纪念日",
                 "永远", "害怕", "孤独", "委屈", "感动", "心跳", "未来", "嫁给", "梦见")
JUDGE_PROMPT = (
    "你是回复规划器。判断这句话该用哪种回复深度："
    "CHAT = 日常闲聊，随性短回复即可；"
    "DEEP = 深情/走心/触景生情的时刻（表白、思念、倾诉心事、深夜emo、纪念日、人生话题），"
    "值得认真写一段较长较深情的回复。只输出 CHAT 或 DEEP 一个词。\n\n他说：%s"
)


async def judge_depth(user_text: str) -> str:
    """返回思考档位：minimal（闲聊，关闭思考）或 high（深情长回复）。

    裁判用硅基流动的 Qwen3-8B（关思考，~1s）；seed-character 做元分类任务不可靠，实测全判 CHAT。
    """
    t = user_text.strip()
    if len(t) <= 10 and not any(k in t for k in DEEP_KEYWORDS):
        return "minimal"
    key = os.getenv("JUDGE_API_KEY")
    if not key:
        return "minimal"
    try:
        sf = AsyncOpenAI(base_url="https://api.siliconflow.cn/v1", api_key=key)
        r = await sf.chat.completions.create(
            model=os.getenv("JUDGE_MODEL", "Qwen/Qwen3-8B"),
            messages=[{"role": "user", "content": JUDGE_PROMPT % t}],
            max_tokens=20,
            extra_body={"enable_thinking": False},
        )
        ans = (r.choices[0].message.content or "").upper()
        result = "high" if "DEEP" in ans else "minimal"
        log.info("judge: %s -> %s", t[:20], result)
        return result
    except Exception:
        log.exception("judge failed, default minimal")
        return "minimal"


TEXT_KEY = re.compile(r'"text"\s*:\s*"')
EMOTION_RE = re.compile(r'"emotion"\s*:\s*"([^"]+)"')
VOICE_RE = re.compile(r'"voice"\s*:\s*"([^"]+)"')


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


async def _stream_once(msgs: list, effort: str, force_reply: bool, result: dict):
    """单轮流式生成。产出 ("emotion", e) / ("voice", v) / ("sentence", s) 事件。

    模型调用行动工具（非 reply）时：不产出任何事件，把工具调用放进
    result["tool_calls"] 由外层执行后重开一轮；正常回复时把最终结果放进
    result["reply"] / result["emotion"]。
    """
    stream = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=msgs,
        tools=gf_tools.TOOL_DEFS + REPLY_TOOL,
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
    emitted = 0  # 已产出的句段数
    emotion_seen: str | None = None
    voice_seen: str | None = None
    diverted = False  # 首个工具不是 reply → 本轮只为取工具结果，不产生事件
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
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


# 模型可调用工具时的系统提示补充
CAPABILITY_NOTE = (
    "\n\n（你有工具可以用：get_current_time 查真实时间（他问时间必须调用，不许自己猜）、"
    "web_search 联网搜索、list_directory / read_file / write_file 浏览和读写服务器上的文件。"
    "需要时先调工具，拿到结果后再调 reply 回复他；工具结果用你自己的话说，别照念。用不上工具就直接 reply。）"
)
MAX_TOOL_ROUNDS = 4


async def chat_stream(user_id: int, user_text: str, recall_task: asyncio.Task | None = None):
    """流式回复生成器：依次产出 ("emotion", e) / ("sentence", s)，最后 ("done", reply, emotion)。

    支持工具调用循环：模型调行动工具 → 执行 → 结果回填 → 重新生成，直到给出 reply。
    recall_task：语音场景下与 ASR 并行的预检索任务。
    """
    store = get_store(user_id)
    judge_task = asyncio.create_task(judge_depth(user_text))
    is_pre_recall = recall_task is not None
    if recall_task is None:
        recall_task = asyncio.create_task(ov_memory.recall(user_text))
    effort, recalled = await asyncio.gather(judge_task, recall_task)
    if not recalled and is_pre_recall:
        recalled = await ov_memory.recall(user_text)  # 预检索为空，用真实文本补一次
    if recalled:
        mem_block = "\n".join(f"- {m}" for m in recalled)
    elif store["memories"]:  # OpenViking 不可用时回退到本地记忆
        mem_block = "\n".join(f"- {m}" for m in store["memories"])
    else:
        mem_block = ""
    system = soul.build_system(mem_block) + CAPABILITY_NOTE
    if effort == "high":
        system += (
            "\n\n（走心時刻：他這句話觸動了你心底最軟的地方。現在拋開平常發短訊息的習慣，"
            "像寫一封短信、一段獨白那樣，把你的心意完整說出來——認真地說 5~8 句、至少 100 字，"
            "回憶你們之間的細節，說你平時不好意思說的話。慢慢說，他會聽完的。"
            "感覺大概是這樣的節奏（只是示範語氣和長度，絕對不要照抄內容，說你自己心裡的話）：「寶貝，你知道嗎……其實我有好多話一直想跟你說……（以下省略）」）"
        )
    log.info("depth=%s", effort)
    msgs = [{"role": "system", "content": system}]
    msgs.extend(store["history"][-HISTORY_TURNS * 2 :])
    msgs.append({"role": "user", "content": user_text})

    result: dict = {}
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        result = {}
        async for ev in _stream_once(msgs, effort, round_no == MAX_TOOL_ROUNDS, result):
            yield ev
        tool_calls = result.get("tool_calls")
        if not tool_calls:
            break
        log.info("tool round %d: %s", round_no, [(t["name"], t["args"]) for t in tool_calls])
        msgs.append({
            "role": "assistant",
            "tool_calls": [{
                "id": t["id"], "type": "function",
                "function": {"name": t["name"], "arguments": json.dumps(t["args"], ensure_ascii=False)},
            } for t in tool_calls],
        })
        for t in tool_calls:
            out = "（先别急着回复，把工具结果用上再说）" if t["name"] == "reply" else await gf_tools.run(t["name"], t["args"])
            msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
    reply = result["reply"]
    emotion = result["emotion"]

    store["history"].append({"role": "user", "content": user_text})
    store["history"].append({"role": "assistant", "content": reply})
    store["history"] = store["history"][-HISTORY_TURNS * 4 :]
    mem.save(user_id, store)
    asyncio.create_task(ov_memory.record_turn(user_id, user_text, reply))
    if not await ov_memory.healthy():  # OV 不在线时用本地提炼兜底
        asyncio.create_task(maybe_extract(user_id))
    yield ("done", reply, emotion)


async def _synth_mp3(text: str, mp3: Path, tts_params: dict | None = None) -> None:
    if os.getenv("DOUBAO_API_KEY"):
        try:
            import tts_seed

            await tts_seed.synth(text, mp3, **(tts_params or {}))
            return
        except Exception:
            log.exception("seed-tts failed, fallback to edge-tts")
    await edge_tts.Communicate(text, TTS_VOICE, rate="+8%").save(str(mp3))


async def tts_to_ogg(text: str, tts_params: dict | None = None) -> Path:
    mp3 = Path(tempfile.mktemp(suffix=".mp3"))
    ogg = Path(tempfile.mktemp(suffix=".ogg"))
    await _synth_mp3(text, mp3, tts_params)
    subprocess.run(
        [FFMPEG, "-y", "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k", str(ogg)],
        check=True, capture_output=True,
    )
    mp3.unlink(missing_ok=True)
    return ogg


def transcribe(path: str) -> str:
    model = get_whisper()
    segments, _ = model.transcribe(path, language="zh", beam_size=1)
    return "".join(seg.text for seg in segments).strip()


# 云端 ASR：主模型 doubao-seed-character 不支持音频输入（实测 400），
# 用方舟音频理解模型转写；失败时回退本地 whisper。设 ASR_MODEL= 可关掉云端转写。
ASR_MODEL = os.getenv("ASR_MODEL", "doubao-seed-2-0-mini-260428")
ASR_PROMPT = "你是语音识别专家。只输出这段语音的转写文本，不要输出任何解释或多余内容；听不清就输出空。"


async def asr_cloud(path: Path) -> str:
    """音频先转 mp3（方舟 input_audio 不支持 ogg），再 base64 传给音频理解模型。"""
    mp3 = path
    tmp = None
    if path.suffix != ".mp3":
        tmp = Path(tempfile.mktemp(suffix=".mp3"))
        subprocess.run([FFMPEG, "-y", "-i", str(path), "-b:a", "32k", str(tmp)],
                       check=True, capture_output=True)
        mp3 = tmp
    try:
        data = base64.b64encode(mp3.read_bytes()).decode()
        r = await llm.chat.completions.create(
            model=ASR_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data, "format": "mp3"}},
                {"type": "text", "text": ASR_PROMPT},
            ]}],
            max_tokens=300,
        )
        return (r.choices[0].message.content or "").strip()
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


async def asr_transcribe(path: str | None = None, url: str | None = None,
                         fmt: str = "ogg", codec: str = "") -> str:
    """语音转文字统一入口：seedasr(URL 直传, 方言/情绪标签) → 方舟音频理解(本地文件) → 本地 whisper。"""
    if url and os.getenv("ASR_SEED", "1") != "0" and os.getenv("DOUBAO_API_KEY"):
        try:
            import asr_seed

            text, hints = await asr_seed.transcribe_url(url, fmt, codec)
            if text:
                return (f"（{'；'.join(hints)}）" if hints else "") + text
        except Exception:
            log.exception("seedasr failed, fallback")
    if path and ASR_MODEL:
        try:
            return await asr_cloud(Path(path))
        except Exception:
            log.exception("cloud asr failed, fallback to whisper")
    if path:
        return await asyncio.to_thread(transcribe, path)
    return ""


SENT_SPLIT = re.compile(r"(?<=[。！？!?；;~…\n])")
SPEAKABLE = re.compile(r"[0-9A-Za-z一-鿿]")
WHOLE_TTS_MAX = 350  # 回复不超过此字数整段一次合成（保情绪细节+语气一致），超过才按句并行


def _speakable(s: str) -> bool:
    """分句碎片里要有真实文字才值得合成——纯标点（如省略号"…"被独立切出）TTS 会返回空音频。"""
    return bool(s.strip()) and bool(SPEAKABLE.search(s))


async def _safe_ogg(sentence: str, tts_params: dict) -> Path | None:
    if not _speakable(sentence):
        return None
    try:
        return await tts_to_ogg(sentence, tts_params)
    except Exception:
        log.exception("tts failed")
        return None


async def _keepalive_action(bot, chat_id: int, action, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, action)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


async def process_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_text: str,
                          recall_task: asyncio.Task | None = None):
    """语音优先：LLM 流式生成。回复不超过 WHOLE_TTS_MAX 字（或悄悄话场景）攒整段一次合成，
    保住省略号等情绪细节且语气一致；超长回复才按句并行合成抢速度。语音按序先发，文字最后到。"""
    chat_id = update.effective_chat.id
    stop = asyncio.Event()
    keepalive = asyncio.create_task(_keepalive_action(ctx.bot, chat_id, ChatAction.RECORD_VOICE, stop))
    emotion = "平静"
    voice_hint = ""
    whisper = False
    pending: list[str] = []  # 未派发的句子缓冲（攒整段用）
    split = False            # 累计超阈值后转逐句并行合成
    tasks: list[asyncio.Task] = []
    full_reply = ""
    t0 = time.time()
    try:
        async for ev in chat_stream(update.effective_user.id, user_text, recall_task=recall_task):
            if ev[0] == "emotion":
                emotion = ev[1]
            elif ev[0] == "voice":
                voice_hint = ev[1]
                whisper = is_whisper(voice_hint)
            elif ev[0] == "sentence":
                if whisper:
                    continue  # 悄悄话攒整段（分句合成气声会逐句漂移）
                if split:
                    tasks.append(asyncio.create_task(_safe_ogg(ev[1], tts_params_for(emotion, voice_hint))))
                else:
                    pending.append(ev[1])
                    if sum(map(len, pending)) > WHOLE_TTS_MAX:
                        split = True
                        for s in pending:
                            tasks.append(asyncio.create_task(_safe_ogg(s, tts_params_for(emotion, voice_hint))))
                        pending.clear()
            else:
                _, full_reply, emotion = ev
    except Exception:
        log.exception("llm failed")
        stop.set()
        await update.message.reply_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    if not split and full_reply:
        tasks.append(asyncio.create_task(_safe_ogg(full_reply, tts_params_for(emotion, voice_hint))))
    log.info("llm stream done in %.1fs, %d sentences, emotion=%s voice=%s split=%s", time.time() - t0, len(tasks), emotion, voice_hint or "-", split)

    oggs = await asyncio.gather(*tasks)
    stop.set()
    keepalive.cancel()
    for ogg in oggs:
        if not ogg:
            continue
        await ctx.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
        await update.message.reply_voice(voice=ogg.read_bytes())
        ogg.unlink(missing_ok=True)
    await update.message.reply_text(full_reply)  # 文字最后到


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _touch_contact(update)
    await process_message(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _touch_contact(update)
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    ogg_in = Path(tempfile.mktemp(suffix=".ogg"))
    try:
        tg_file = await update.message.voice.get_file()
        # seedasr 可直接拉取 TG 文件链接，无需本地下载；本地文件留作降级链路
        file_url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{tg_file.file_path}"
        await tg_file.download_to_drive(str(ogg_in))
        # ASR 与记忆预检索并行：whisper 识别的同时，用最近对话上下文先做语义检索
        store = get_store(update.effective_user.id)
        ctx_query = " ".join(m["content"] for m in store["history"][-2:] if m.get("content"))
        pre_recall = asyncio.create_task(ov_memory.recall(ctx_query)) if ctx_query else None
        user_text = await asr_transcribe(str(ogg_in), url=file_url, fmt="ogg", codec="opus")
        log.info("asr: %s", user_text)
        if not user_text:
            if pre_recall:
                pre_recall.cancel()
            await update.message.reply_text("欸？人家沒聽清楚啦，你再說一次嘛～")
            return
    except Exception:
        log.exception("asr failed")
        await update.message.reply_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    finally:
        ogg_in.unlink(missing_ok=True)
    await process_message(update, ctx, user_text, recall_task=pre_recall)


def _touch_contact(update: Update) -> None:
    _save_contact(update.effective_user.id, update.effective_chat.id)


async def heartbeat_loop(send) -> None:
    """静默契约：没事就 NO_REPLY，绝不打扰；不想打扰但有话想说时，可以写进小本本（tinynote/）。

    send(chat_id, text, ogg)：平台相关的主动发消息回调，ogg 为语音文件路径（TTS 失败时为 None）。
    """
    await asyncio.sleep(120)  # 启动后先等两分钟
    while True:
        try:
            contact = _load_contact()
            if not contact:
                continue
            if contact.get("platform", "telegram") != BOT_PLATFORM:
                continue  # 最后在另一个平台聊的，不在本平台打扰
            now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))  # 服务器是 UTC，他在北京时间
            if not (ACTIVE_HOURS[0] <= now_dt.hour < ACTIVE_HOURS[1]):
                continue
            silence_h = (time.time() - contact.get("ts", 0)) / 3600
            if silence_h < HEARTBEAT_SILENCE_H:
                continue
            uid = contact["user_id"]
            store = get_store(uid)
            recalled = await ov_memory.recall("最近关心他、问候他、约定、他的近况")
            mem_block = "\n".join(f"- {m}" for m in recalled or store["memories"])
            system = soul.build_system(mem_block)
            now = now_dt.strftime("%H:%M")
            msgs = [
                {"role": "system", "content": system},
                *store["history"][-HISTORY_TURNS * 2 :],
                {"role": "user", "content": (
                    f"（系统提示：现在是{now}，他已经{silence_h:.1f}小时没和你说话了。"
                    "如果你想主动关心他，就调用 reply 工具发一条消息；"
                    "如果不想打扰他、但心里有话想说，可以写进你的小本本"
                    "（用 write_file 写到 ~/cyber-gf/tinynote/，比如日记、随笔，写之前可以先用 list_directory 看看以前写过什么）；"
                    "如果什么都不想做（比如刚聊过不久、没有理由打扰），就只回复 NO_REPLY，什么也别发。）"
                )},
            ]
            text = ""
            emotion = "温柔"
            for _ in range(3):  # 行动工具（写/翻小本本）最多循环 3 轮，reply 或 NO_REPLY 收尾
                resp = await llm.chat.completions.create(
                    model=LLM_MODEL, messages=msgs, tools=REPLY_TOOL + HEARTBEAT_TOOLS, tool_choice="auto",
                )
                m = resp.choices[0].message
                if not m.tool_calls:
                    break  # NO_REPLY
                calls = m.tool_calls
                reply_call = next((c for c in calls if c.function.name == "reply"), None)
                if reply_call:
                    args = json.loads(reply_call.function.arguments)
                    text = (args.get("text") or "").strip()
                    emotion = args.get("emotion") or "温柔"
                    break
                msgs.append({
                    "role": "assistant",
                    "tool_calls": [{"id": c.id, "type": "function",
                                    "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                   for c in calls],
                })
                for c in calls:
                    try:
                        targs = json.loads(c.function.arguments or "{}")
                    except json.JSONDecodeError:
                        targs = {}
                    out = await gf_tools.run(c.function.name, targs)
                    log.info("heartbeat tool: %s(%s) -> %s", c.function.name, targs, out[:60])
                    msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})
            if not text:
                continue  # NO_REPLY 或只写了小本本，不打扰他
            log.info("heartbeat: reaching out (%s): %s", emotion, text[:50])
            store["history"].append({"role": "assistant", "content": text})
            mem.save(uid, store)
            ogg = None
            try:
                ogg = await tts_to_ogg(text, tts_params_for(emotion))
            except Exception:
                log.exception("heartbeat tts failed")
            try:
                await send(contact["chat_id"], text, ogg)
            except Exception:
                log.exception("heartbeat send failed")
            if ogg:
                ogg.unlink(missing_ok=True)
            contact["ts"] = time.time()
            _save_contact(uid, contact["chat_id"], BOT_PLATFORM)
        except Exception:
            log.exception("heartbeat error")
        finally:
            await asyncio.sleep(HEARTBEAT_MINUTES * 60)


async def _post_init(app: Application) -> None:
    # 过滤 httpcore2 在 Python 3.14 下关闭流式响应时的已知噪音
    def _exc_filter(loop, context):
        if "generator didn't stop after athrow" in str(context.get("exception", "")):
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_exc_filter)

    async def tg_send(chat_id: int, text: str, ogg: Path | None) -> None:
        await app.bot.send_message(chat_id, text)
        if ogg:
            await app.bot.send_voice(chat_id, voice=ogg.read_bytes())

    asyncio.create_task(heartbeat_loop(tg_send))


def run_telegram() -> None:
    app = Application.builder().token(TG_TOKEN).post_init(_post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    log.info("bot started (telegram)")
    app.run_polling()


def main() -> None:
    if BOT_PLATFORM == "discord":
        import discord_bot

        discord_bot.run()
    else:
        run_telegram()


if __name__ == "__main__":
    main()
