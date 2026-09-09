"""platform_telegram 插件：Telegram 接入（ptb 手动生命周期，复用 core 统一事件循环）。

不用 run_polling（它自建事件循环）：apply 时注册 "ready" 事件监听，core 加载完全部插件
emit ready 后才 ctx.create_task 启动 initialize → start → updater.start_polling()；
dispose 时逆序 updater.stop() → app.stop() → app.shutdown()（on_dispose 在 task 取消前执行）。

provide "platform" 服务：async send(chat_id, text, ogg)——主动发文字+可选语音（heartbeat 消费）。
消息处理：文字/语音 → chat.process(user_id, text, ui)，ui 协议实现见 TelegramUI。
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

log = logging.getLogger("cyber-gf.telegram")

requires = ["chat", "sessions", "asr", "memory"]
provides = ["platform"]


class TelegramUI:
    """chat.process 的 ui 协议实现（Telegram 版）。"""

    def __init__(self, update: Update, bot):
        self._message = update.message
        self._bot = bot
        self._chat_id = update.effective_chat.id

    async def send_text(self, text: str) -> None:
        await self._message.reply_text(text)

    async def send_voice(self, ogg_path) -> None:
        await self._message.reply_voice(voice=Path(ogg_path).read_bytes())

    async def status(self, kind) -> None:
        pass  # Telegram 没有可用的自定义状态，忽略

    async def pulse(self, kind: str) -> None:
        # 生成中/发语音前都显示"正在录音"（原 _keepalive_action 的实际行为）
        action = ChatAction.RECORD_VOICE if kind == "record" else ChatAction.TYPING
        await self._bot.send_chat_action(self._chat_id, action)


class TelegramPlatform:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._token = cfg.tg_token
        self._app: Application | None = None

    # ---- platform 服务：heartbeat 主动发消息（原 tg_send） ----

    async def send(self, chat_id: int, text: str, ogg=None) -> None:
        await self._app.bot.send_message(chat_id, text)
        if ogg:
            await self._app.bot.send_voice(chat_id, voice=Path(ogg).read_bytes())

    # ---- 消息入口（原 bot.py 的 on_text / on_voice / _touch_contact） ----

    def _touch_contact(self, update: Update) -> None:
        self._ctx.inject("sessions").save_contact(
            "telegram", update.effective_user.id, update.effective_chat.id)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._touch_contact(update)
        await self._ctx.inject("chat").process(
            update.effective_user.id, update.message.text, TelegramUI(update, ctx.bot))

    async def on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._touch_contact(update)
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        ogg_in = Path(tempfile.mktemp(suffix=".ogg"))
        pre_recall = None
        try:
            tg_file = await update.message.voice.get_file()
            # seedasr 可直接拉取 TG 文件链接，无需本地下载；本地文件留作降级链路
            file_url = f"https://api.telegram.org/file/bot{self._token}/{tg_file.file_path}"
            await tg_file.download_to_drive(str(ogg_in))
            # ASR 与记忆预检索并行：识别的同时，用最近对话上下文先做语义检索
            store = self._ctx.inject("sessions").get_store(update.effective_user.id)
            ctx_query = " ".join(m["content"] for m in store["history"][-2:] if m.get("content"))
            memory = self._ctx.inject("memory")
            pre_recall = asyncio.create_task(memory.recall(ctx_query)) if ctx_query else None
            text, hints = await self._ctx.inject("asr").transcribe(
                url=file_url, path=str(ogg_in), fmt="ogg", codec="opus")
            hint_list = hints.get("hints") or []
            user_text = (f"（{'；'.join(hint_list)}）" if hint_list else "") + text
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
        await self._ctx.inject("chat").process(
            update.effective_user.id, user_text, TelegramUI(update, ctx.bot),
            recall_task=pre_recall)

    # ---- 生命周期 ----

    async def run(self) -> None:
        app = Application.builder().token(self._token).build()
        self._app = app
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        log.info("bot started (telegram)")

    async def shutdown(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception:
            log.exception("telegram shutdown failed")


def apply(ctx) -> None:
    # 过滤 httpcore2 在 Python 3.14 下关闭流式响应时的已知噪音（原 _post_init）
    def _exc_filter(loop, context):
        if "generator didn't stop after athrow" in str(context.get("exception", "")):
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_exc_filter)

    platform = TelegramPlatform(ctx)
    ctx.provide("platform", platform)

    def _on_ready():
        ctx.create_task(platform.run())

    ctx.on("ready", _on_ready)
    ctx.on_dispose(platform.shutdown)
