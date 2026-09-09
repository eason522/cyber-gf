"""platform_discord 插件：Discord 接入（手动生命周期 client.start(token) 作为 task，不用 client.run）。

私信或 @机器人 触发；语音以 ogg 音频附件发送（Discord 机器人不能发原生语音条），
收语音靠音频附件；aiohttp 不读代理环境变量，proxy 显式传参。
provide "platform" 服务：async send(chat_id, text, ogg)（heartbeat 消费）。
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import discord

log = logging.getLogger("cyber-gf.discord")

requires = ["chat", "sessions", "asr", "memory"]
provides = ["platform"]

STATUS_TEXT = {"recall": "正在回忆…", "surf": "正在刷小红书…"}


async def _send_text(channel, text: str) -> None:
    for i in range(0, len(text), 1990):
        await channel.send(text[i : i + 1990])


class DiscordUI:
    """chat.process 的 ui 协议实现（Discord 版）。"""

    def __init__(self, message: discord.Message, client: discord.Client):
        self._channel = message.channel
        self._client = client

    async def send_text(self, text: str) -> None:
        await _send_text(self._channel, text)

    async def send_voice(self, ogg_path) -> None:
        await self._channel.send(file=discord.File(str(ogg_path), filename="voice.ogg"))

    async def status(self, kind) -> None:
        """按 chat 的状态事件切换 bot 自定义状态（成员列表/资料卡可见），None 清除。"""
        try:
            await self._client.change_presence(
                activity=discord.CustomActivity(name=STATUS_TEXT[kind]) if kind else None
            )
        except Exception:
            pass

    async def pulse(self, kind: str) -> None:
        await self._channel.typing()


class DiscordPlatform:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._token = cfg.discord_token
        intents = discord.Intents.default()
        intents.message_content = True
        # aiohttp 默认不读代理环境变量，网络走代理必须显式传
        self._client = discord.Client(
            intents=intents, proxy=os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"))
        self._client.event(self.on_message)
        self._client.event(self.on_ready)

    # ---- platform 服务：heartbeat 主动发消息（原 _dc_send） ----

    async def send(self, chat_id: int, text: str, ogg=None) -> None:
        channel = self._client.get_channel(chat_id) or await self._client.fetch_channel(chat_id)
        await _send_text(channel, text)
        if ogg:
            await channel.send(file=discord.File(str(ogg), filename="voice.ogg"))

    # ---- 消息入口（原 discord_bot.py 的 on_message / _on_audio） ----

    def _touch_contact(self, message: discord.Message) -> None:
        self._ctx.inject("sessions").save_contact("discord", message.author.id, message.channel.id)

    async def on_ready(self) -> None:
        log.info("bot started (discord), user=%s", self._client.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = self._client.user in message.mentions
        if not (is_dm or mentioned):
            return
        self._touch_contact(message)
        for att in message.attachments:
            if (att.content_type or "").startswith("audio/"):
                await self._on_audio(message, att)
                return
        text = message.content
        if mentioned:
            text = text.replace(f"<@{self._client.user.id}>", "").replace(f"<@!{self._client.user.id}>", "").strip()
        if not text:
            return
        await self._ctx.inject("chat").process(
            message.author.id, text, DiscordUI(message, self._client))

    async def _on_audio(self, message: discord.Message, attachment: discord.Attachment) -> None:
        audio_in = Path(tempfile.mktemp(suffix=".ogg"))
        pre_recall = None
        try:
            await attachment.save(str(audio_in))  # 本地文件留作降级链路
            # 按附件类型推断 seedasr 的 format/codec（语音消息通常是 ogg opus）
            ct = (attachment.content_type or "").lower()
            ext = (attachment.filename or "").rsplit(".", 1)[-1].lower()
            fmt = {"mpeg": "mp3", "mp3": "mp3", "x-m4a": "m4a", "m4a": "m4a", "wav": "wav"}.get(
                ct.removeprefix("audio/"), ext if ext in ("mp3", "wav", "m4a", "aac") else "ogg")
            codec = "opus" if fmt == "ogg" else ""
            # ASR 与记忆预检索并行（同 Telegram 路径）
            store = self._ctx.inject("sessions").get_store(message.author.id)
            ctx_query = " ".join(m["content"] for m in store["history"][-2:] if m.get("content"))
            memory = self._ctx.inject("memory")
            pre_recall = asyncio.create_task(memory.recall(ctx_query)) if ctx_query else None
            text, hints = await self._ctx.inject("asr").transcribe(
                url=attachment.url, path=str(audio_in), fmt=fmt, codec=codec)
            hint_list = hints.get("hints") or []
            user_text = (f"（{'；'.join(hint_list)}）" if hint_list else "") + text
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
        await self._ctx.inject("chat").process(
            message.author.id, user_text, DiscordUI(message, self._client),
            recall_task=pre_recall)

    # ---- 生命周期 ----

    async def run(self) -> None:
        await self._client.start(self._token)

    async def shutdown(self) -> None:
        try:
            await self._client.close()
        except Exception:
            log.exception("discord shutdown failed")


def apply(ctx) -> None:
    ctx.inject("config")
    platform = DiscordPlatform(ctx)
    ctx.provide("platform", platform)

    def _on_ready():
        ctx.create_task(platform.run())

    ctx.on("ready", _on_ready)
    ctx.on_dispose(platform.shutdown)
