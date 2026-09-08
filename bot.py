import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

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
ACTIVE_HOURS = (8, 23)  # 深夜免打扰
CONTACT_FILE = Path(__file__).parent / "data" / "contact.json"


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
                "text": {"type": "string", "description": "口语台词：日常闲聊 1~3 句；系统提示走心时刻时，写 5~8 句的深情段落（至少100字），把心意说完整"},
            },
            "required": ["emotion", "text"],
        },
    },
}]

EMOTIONS = {
    "撒娇": {"context": "用撒娇、软软的语气说话", "speech_rate": -5, "pitch": 2},
    "温柔": {"context": "用温柔、轻声的语气说话", "speech_rate": -8, "pitch": 0},
    "开心": {"context": "用开心、轻快的语气说话", "speech_rate": 8, "pitch": 2},
    "难过": {"context": "用难过、委屈的语气说话", "speech_rate": -10, "pitch": -2},
    "生气": {"context": "用有点生气、闹别扭的语气说话", "speech_rate": 5, "pitch": 1},
    "害羞": {"context": "用害羞、轻声细语的语气说话", "speech_rate": -5, "pitch": 1},
    "平静": {"context": "", "speech_rate": 0, "pitch": 0},
}

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


async def chat_stream(user_id: int, user_text: str, recall_task: asyncio.Task | None = None):
    """流式回复生成器：依次产出 ("emotion", e) / ("sentence", s)，最后 ("done", reply, emotion)。

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
    system = soul.build_system(mem_block)
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
    stream = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=msgs,
        tools=REPLY_TOOL,
        tool_choice={"type": "function", "function": {"name": "reply"}},
        stream=True,
        extra_body=(
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
            if effort == "high"
            else {"thinking": {"type": "disabled"}}
        ),
    )
    raw = ""  # 工具调用参数（累积的 JSON 字符串）
    plain = ""  # 模型没走工具调用时的兜底
    emitted = 0  # 已产出的句段数
    emotion_seen: str | None = None
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            tc = getattr(delta, "tool_calls", None)
            if tc:
                raw += tc[0].function.arguments or ""
            elif delta.content:
                plain += delta.content
            if raw and emotion_seen is None:
                em = EMOTION_RE.search(raw)
                if em:
                    emotion_seen = em.group(1)
                    yield ("emotion", emotion_seen)
            decoded = _extract_stream_text(raw) if raw else plain
            parts = SENT_SPLIT.split(decoded)
            complete = [p.strip() for p in parts[:-1] if p.strip()]
            while emitted < len(complete):
                yield ("sentence", complete[emitted])
                emitted += 1
    finally:
        try:
            await stream.close()
        except Exception:
            pass

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
    rest = [p.strip() for p in SENT_SPLIT.split(decoded) if p.strip()]
    while emitted < len(rest):
        yield ("sentence", rest[emitted])
        emitted += 1

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


SENT_SPLIT = re.compile(r"(?<=[。！？!?；;~…\n])")


async def _safe_ogg(sentence: str, tts_params: dict) -> Path | None:
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
    """语音优先：LLM 流式生成，句子一完整就并行合成，语音条按序先发，完整文字最后发。"""
    chat_id = update.effective_chat.id
    stop = asyncio.Event()
    keepalive = asyncio.create_task(_keepalive_action(ctx.bot, chat_id, ChatAction.RECORD_VOICE, stop))
    emotion = "平静"
    tasks: list[asyncio.Task] = []
    full_reply = ""
    t0 = time.time()
    try:
        async for ev in chat_stream(update.effective_user.id, user_text, recall_task=recall_task):
            if ev[0] == "emotion":
                emotion = ev[1]
            elif ev[0] == "sentence":
                tasks.append(asyncio.create_task(_safe_ogg(ev[1], EMOTIONS[emotion])))
            else:
                _, full_reply, emotion = ev
    except Exception:
        log.exception("llm failed")
        stop.set()
        await update.message.reply_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    log.info("llm stream done in %.1fs, %d sentences, emotion=%s", time.time() - t0, len(tasks), emotion)

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
        await tg_file.download_to_drive(str(ogg_in))
        # ASR 与记忆预检索并行：whisper 识别的同时，用最近对话上下文先做语义检索
        store = get_store(update.effective_user.id)
        ctx_query = " ".join(m["content"] for m in store["history"][-2:] if m.get("content"))
        pre_recall = asyncio.create_task(ov_memory.recall(ctx_query)) if ctx_query else None
        user_text = await asyncio.to_thread(transcribe, str(ogg_in))
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
    """静默契约：没事就 NO_REPLY，绝不打扰。

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
            hour = time.localtime().tm_hour
            if not (ACTIVE_HOURS[0] <= hour < ACTIVE_HOURS[1]):
                continue
            silence_h = (time.time() - contact.get("ts", 0)) / 3600
            if silence_h < HEARTBEAT_SILENCE_H:
                continue
            uid = contact["user_id"]
            store = get_store(uid)
            recalled = await ov_memory.recall("最近关心他、问候他、约定、他的近况")
            mem_block = "\n".join(f"- {m}" for m in recalled or store["memories"])
            system = soul.build_system(mem_block)
            now = time.strftime("%H:%M")
            msgs = [
                {"role": "system", "content": system},
                *store["history"][-HISTORY_TURNS * 2 :],
                {"role": "user", "content": (
                    f"（系统提示：现在是{now}，他已经{silence_h:.1f}小时没和你说话了。"
                    "如果你想主动关心他，就调用 reply 工具发一条消息；"
                    "如果没有特别想说的（比如刚聊过不久、没有理由打扰），就只回复 NO_REPLY，什么也别发。）"
                )},
            ]
            resp = await llm.chat.completions.create(
                model=LLM_MODEL, messages=msgs, tools=REPLY_TOOL, tool_choice="auto",
            )
            m = resp.choices[0].message
            if not m.tool_calls:
                continue  # NO_REPLY
            args = json.loads(m.tool_calls[0].function.arguments)
            text = (args.get("text") or "").strip()
            emotion = args.get("emotion") or "温柔"
            if not text:
                continue
            log.info("heartbeat: reaching out (%s): %s", emotion, text[:50])
            store["history"].append({"role": "assistant", "content": text})
            mem.save(uid, store)
            ogg = None
            try:
                ogg = await tts_to_ogg(text, EMOTIONS.get(emotion, EMOTIONS["平静"]))
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
