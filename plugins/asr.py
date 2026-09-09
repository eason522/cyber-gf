"""asr 插件：语音转文字三级降级链（原 bot.py 的 asr_transcribe，逻辑逐字保留）：

seedasr.auc 录音识别 2.0（URL 直传，方言/情绪标签，委托 asr_seed.py）
→ 方舟音频理解模型（本地文件转 mp3 后 base64）
→ 本地 faster-whisper（CPU，惰性单例兜底）。
"""

import asyncio
import base64
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("cyber-gf.asr")

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = str(BASE_DIR / "ffmpeg")

ASR_PROMPT = "你是语音识别专家。只输出这段语音的转写文本，不要输出任何解释或多余内容；听不清就输出空。"

requires = ["llm"]
provides = ["asr"]


class ASRService:
    def __init__(self, ctx):
        self._ctx = ctx
        cfg = ctx.inject("config")
        self._model = cfg.asr_model
        self._seed_enabled = cfg.asr_seed != "0"
        self._doubao_key = cfg.doubao_api_key
        self._whisper_name = cfg.whisper_model
        self._whisper = None

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper model %s ...", self._whisper_name)
            self._whisper = WhisperModel(self._whisper_name, device="cpu", compute_type="int8")
        return self._whisper

    def _transcribe_local(self, path: str) -> str:
        model = self._get_whisper()
        segments, _ = model.transcribe(path, language="zh", beam_size=1)
        return "".join(seg.text for seg in segments).strip()

    async def _asr_cloud(self, path: Path) -> str:
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
            llm = self._ctx.inject("llm")
            r = await llm.chat.completions.create(
                model=self._model,
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

    async def transcribe(self, url: str | None = None, path: str | None = None,
                         fmt: str = "ogg", codec: str = "") -> tuple[str, dict]:
        """语音转文字统一入口。返回 (文本, hints)，hints 含 seedasr 的情绪/方言提示列表。"""
        if url and self._seed_enabled and self._doubao_key:
            try:
                import asr_seed

                text, hint_list = await asr_seed.transcribe_url(url, fmt, codec)
                if text:
                    return text, {"hints": hint_list}
            except Exception:
                log.exception("seedasr failed, fallback")
        if path and self._model:
            try:
                return await self._asr_cloud(Path(path)), {"hints": []}
            except Exception:
                log.exception("cloud asr failed, fallback to whisper")
        if path:
            return await asyncio.to_thread(self._transcribe_local, path), {"hints": []}
        return "", {"hints": []}


def apply(ctx) -> None:
    ctx.inject("config")
    ctx.provide("asr", ASRService(ctx))
