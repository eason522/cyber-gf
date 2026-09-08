"""Discord 接入层：复用 bot.py 的核心流水线（深度路由/流式编排/TTS/记忆/心跳）。

由 bot.main() 按 BOT_PLATFORM=discord 分发到这里，与 Telegram 二选一运行。
语音以音频文件附件发送（Discord 机器人无法发原生语音条），可内联播放。
"""

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

import discord

import bot
import ov_memory

log = logging.getLogger("cyber-gf.discord")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True
# aiohttp 默认不读代理环境变量，网络走代理必须显式传
client = discord.Client(intents=intents, proxy=os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"))


def _touch_contact(message: discord.Message) -> None:
    bot._save_contact(message.author.id, message.channel.id, "discord")


async def _keepalive_typing(channel, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await channel.typing()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=8)
        except asyncio.TimeoutError:
            pass


async def _send_text(channel, text: str) -> None:
    for i in range(0, len(text), 1990):
        await channel.send(text[i : i + 1990])


async def process_message(message: discord.Message, user_text: str,
                          recall_task: asyncio.Task | None = None):
    """与 bot.process_message 同构：流式生成，句子一完整就并行合成，语音按序先发，完整文字最后到。"""
    channel = message.channel
    stop = asyncio.Event()
    keepalive = asyncio.create_task(_keepalive_typing(channel, stop))
    emotion = "平静"
    tasks: list[asyncio.Task] = []
    full_reply = ""
    t0 = time.time()
    try:
        async for ev in bot.chat_stream(message.author.id, user_text, recall_task=recall_task):
            if ev[0] == "emotion":
                emotion = ev[1]
            elif ev[0] == "sentence":
                tasks.append(asyncio.create_task(bot._safe_ogg(ev[1], bot.EMOTIONS[emotion])))
            else:
                _, full_reply, emotion = ev
    except Exception:
        log.exception("llm failed")
        stop.set()
        keepalive.cancel()
        await channel.send("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    log.info("llm stream done in %.1fs, %d sentences, emotion=%s", time.time() - t0, len(tasks), emotion)

    oggs = await asyncio.gather(*tasks)
    stop.set()
    keepalive.cancel()
    for ogg in oggs:
        if not ogg:
            continue
        await channel.typing()
        await channel.send(file=discord.File(str(ogg), filename="voice.ogg"))
        ogg.unlink(missing_ok=True)
    await _send_text(channel, full_reply)  # 文字最后到


async def _on_audio(message: discord.Message, attachment: discord.Attachment):
    audio_in = Path(tempfile.mktemp(suffix=".ogg"))
    try:
        await attachment.save(str(audio_in))  # 本地文件留作降级链路
        # 按附件类型推断 seedasr 的 format/codec（语音消息通常是 ogg opus）
        ct = (attachment.content_type or "").lower()
        ext = (attachment.filename or "").rsplit(".", 1)[-1].lower()
        fmt = {"mpeg": "mp3", "mp3": "mp3", "x-m4a": "m4a", "m4a": "m4a", "wav": "wav"}.get(
            ct.removeprefix("audio/"), ext if ext in ("mp3", "wav", "m4a", "aac") else "ogg")
        codec = "opus" if fmt == "ogg" else ""
        # ASR 与记忆预检索并行（同 Telegram 路径）
        store = bot.get_store(message.author.id)
        ctx_query = " ".join(m["content"] for m in store["history"][-2:] if m.get("content"))
        pre_recall = asyncio.create_task(ov_memory.recall(ctx_query)) if ctx_query else None
        user_text = await bot.asr_transcribe(str(audio_in), url=attachment.url, fmt=fmt, codec=codec)
        log.info("asr: %s", user_text)
        if not user_text:
            if pre_recall:
                pre_recall.cancel()
            await message.channel.send("欸？人家沒聽清楚啦，你再說一次嘛～")
            return
    except Exception:
        log.exception("asr failed")
        await message.channel.send("嗚…人家剛剛恍神了啦，你再說一次好不好齁🥺")
        return
    finally:
        audio_in.unlink(missing_ok=True)
    await process_message(message, user_text, recall_task=pre_recall)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = client.user in message.mentions
    if not (is_dm or mentioned):
        return
    _touch_contact(message)
    for att in message.attachments:
        if (att.content_type or "").startswith("audio/"):
            await _on_audio(message, att)
            return
    text = message.content
    if mentioned:
        text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    if not text:
        return
    await process_message(message, text)


async def _dc_send(chat_id: int, text: str, ogg: Path | None) -> None:
    channel = client.get_channel(chat_id) or await client.fetch_channel(chat_id)
    await _send_text(channel, text)
    if ogg:
        await channel.send(file=discord.File(str(ogg), filename="voice.ogg"))


@client.event
async def on_ready():
    log.info("bot started (discord), user=%s", client.user)
    asyncio.create_task(bot.heartbeat_loop(_dc_send))


def run() -> None:
    client.run(DISCORD_TOKEN, log_handler=None)
