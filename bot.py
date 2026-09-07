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

TG_TOKEN = os.environ["TG_TOKEN"]
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


def _save_contact(user_id: int, chat_id: int) -> None:
    CONTACT_FILE.parent.mkdir(exist_ok=True)
    CONTACT_FILE.write_text(json.dumps({"user_id": user_id, "chat_id": chat_id, "ts": time.time()}))

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
                "text": {"type": "string"},
                "emotion": {
                    "type": "string",
                    "enum": ["撒娇", "温柔", "开心", "难过", "生气", "害羞", "平静"],
                },
            },
            "required": ["text", "emotion"],
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


async def chat(user_id: int, user_text: str) -> tuple[str, str]:
    store = get_store(user_id)
    recalled = await ov_memory.recall(user_text)
    if recalled:
        mem_block = "\n".join(f"- {m}" for m in recalled)
    elif store["memories"]:  # OpenViking 不可用时回退到本地记忆
        mem_block = "\n".join(f"- {m}" for m in store["memories"])
    else:
        mem_block = ""
    system = soul.build_system(mem_block)
    msgs = [{"role": "system", "content": system}]
    msgs.extend(store["history"][-HISTORY_TURNS * 2 :])
    msgs.append({"role": "user", "content": user_text})
    resp = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=msgs,
        tools=REPLY_TOOL,
        tool_choice={"type": "function", "function": {"name": "reply"}},
    )
    m = resp.choices[0].message
    emotion = "平静"
    if m.tool_calls:
        args = json.loads(m.tool_calls[0].function.arguments)
        reply = (args.get("text") or "").strip()
        emotion = args.get("emotion") or emotion
    else:
        reply = (m.content or "").strip()
    if not reply:
        raise ValueError("empty reply")
    if emotion not in EMOTIONS:
        emotion = "平静"
    store["history"].append({"role": "user", "content": user_text})
    store["history"].append({"role": "assistant", "content": reply})
    store["history"] = store["history"][-HISTORY_TURNS * 4 :]
    mem.save(user_id, store)
    asyncio.create_task(ov_memory.record_turn(user_id, user_text, reply))
    if not await ov_memory.healthy():  # OV 不在线时用本地提炼兜底
        asyncio.create_task(maybe_extract(user_id))
    return reply, emotion


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


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT.split(text) if p.strip()]
    merged: list[str] = []
    for p in parts:
        if merged and len(p) < 6:
            merged[-1] += p
        elif merged and len(merged[-1]) < 6:
            merged[-1] += p
        else:
            merged.append(p)
    return merged or [text]


async def reply_with_text_and_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE, reply: str, emotion: str):
    await update.message.reply_text(reply)
    chat_id = update.effective_chat.id
    tts_params = EMOTIONS[emotion]
    log.info("emotion=%s sentences=%d", emotion, len(split_sentences(reply)))

    async def make_ogg(s: str) -> Path | None:
        try:
            return await tts_to_ogg(s, tts_params)
        except Exception:
            log.exception("tts failed")
            return None

    oggs = await asyncio.gather(*(make_ogg(s) for s in split_sentences(reply)))
    for ogg in oggs:
        if not ogg:
            continue
        await ctx.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
        await update.message.reply_voice(voice=ogg.read_bytes())
        ogg.unlink(missing_ok=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    _touch_contact(update)
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        reply, emotion = await chat(update.effective_user.id, user_text)
    except Exception:
        log.exception("llm failed")
        await update.message.reply_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    await reply_with_text_and_voice(update, ctx, reply, emotion)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _touch_contact(update)
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    ogg_in = Path(tempfile.mktemp(suffix=".ogg"))
    try:
        tg_file = await update.message.voice.get_file()
        await tg_file.download_to_drive(str(ogg_in))
        user_text = await asyncio.to_thread(transcribe, str(ogg_in))
        log.info("asr: %s", user_text)
        if not user_text:
            await update.message.reply_text("欸？人家沒聽清楚啦，你再說一次嘛～")
            return
        reply, emotion = await chat(update.effective_user.id, user_text)
    except Exception:
        log.exception("voice pipeline failed")
        await update.message.reply_text("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    finally:
        ogg_in.unlink(missing_ok=True)
    await reply_with_text_and_voice(update, ctx, reply, emotion)


def _touch_contact(update: Update) -> None:
    _save_contact(update.effective_user.id, update.effective_chat.id)


async def heartbeat_loop(app: Application) -> None:
    """静默契约：没事就 NO_REPLY，绝不打扰。"""
    await asyncio.sleep(120)  # 启动后先等两分钟
    while True:
        try:
            contact = _load_contact()
            if not contact:
                continue
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
            chat_id = contact["chat_id"]
            await app.bot.send_message(chat_id, text)
            tts_params = EMOTIONS.get(emotion, EMOTIONS["平静"])
            try:
                ogg = await tts_to_ogg(text, tts_params)
                await app.bot.send_voice(chat_id, voice=ogg.read_bytes())
                ogg.unlink(missing_ok=True)
            except Exception:
                log.exception("heartbeat tts failed")
            contact["ts"] = time.time()
            _save_contact(uid, chat_id)
        except Exception:
            log.exception("heartbeat error")
        finally:
            await asyncio.sleep(HEARTBEAT_MINUTES * 60)


async def _post_init(app: Application) -> None:
    asyncio.create_task(heartbeat_loop(app))


def main():
    app = Application.builder().token(TG_TOKEN).post_init(_post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    log.info("bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
