"""tts 插件：seed-tts-2.0 → edge-tts 两级降级合成 + 情绪/音色映射 + ogg 转换。

自 bot.py 搬入，行为逐字一致：EMOTIONS 指令措辞、悄悄话切 vv 音色 + 纯气声指令、
整段/分句阈值 WHOLE_TTS_MAX、edge-tts 兜底 rate="+8%"、ffmpeg 用项目根目录
./ffmpeg 静态二进制。seed-tts 协议实现委托 tts_seed.py 库。
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import edge_tts

log = logging.getLogger("cyber-gf.tts")

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = str(BASE_DIR / "ffmpeg")

requires: list[str] = []
provides = ["tts"]

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
# 气声会逐句漂移，context_texts 混入 quote/前句/section_id 也会稀释指令，隔离实验实测）。
# 其余情绪都用默认音色小和（自带台湾腔）
WHISPER_KEYWORDS = ("耳语", "悄悄话", "气声", "asmr")
WHISPER_VOICE = "zh_female_vv_uranus_bigtts"
WHISPER_INSTRUCTION = ("全程用纯气声耳语：声带完全不震动、没有一点真声和音调起伏，"
                       "只有气流摩擦的沙沙声，放慢语速、贴着耳朵轻轻地说")

WHOLE_TTS_MAX = 350  # 回复不超过此字数整段一次合成（保情绪细节+语气一致），超过才按句并行

SPEAKABLE = re.compile(r"[0-9A-Za-z一-鿿]")


class TTSService:
    WHOLE_TTS_MAX = WHOLE_TTS_MAX

    def __init__(self, ctx):
        cfg = ctx.inject("config")
        self._doubao_key = cfg.doubao_api_key
        self._fallback_voice = cfg.tts_voice

    def is_whisper(self, voice_hint: str) -> bool:
        return any(k in voice_hint.lower() for k in WHISPER_KEYWORDS)

    def params_for(self, emotion: str, voice_hint: str = "") -> dict:
        """语音指令走 additions.context_texts，且只放一条纯指令——混入引用上文/多条指令
        叠加都会稀释效果（隔离实验实测）。用户原话绝不进 context（同样会干扰）。"""
        hint = voice_hint.strip()
        if self.is_whisper(hint):
            return {"context": [WHISPER_INSTRUCTION], "voice": WHISPER_VOICE}
        instruction = "；".join(c for c in (EMOTIONS.get(emotion, ""), hint) if c)
        return {"context": [instruction] if instruction else []}

    async def synth(self, text: str, out_mp3, *, emotion: str | None = None,
                    voice: str | None = None) -> None:
        """合成 mp3：有 DOUBAO_API_KEY 走 seed-tts-2.0（失败降级），否则 edge-tts。"""
        tts_params = None if emotion is None and voice is None else self.params_for(emotion or "", voice or "")
        await self._synth_mp3(text, Path(out_mp3), tts_params)

    async def _synth_mp3(self, text: str, mp3: Path, tts_params: dict | None = None) -> None:
        if self._doubao_key:
            try:
                import tts_seed

                await tts_seed.synth(text, mp3, **(tts_params or {}))
                return
            except Exception:
                log.exception("seed-tts failed, fallback to edge-tts")
        await edge_tts.Communicate(text, self._fallback_voice, rate="+8%").save(str(mp3))

    async def to_ogg(self, src_path) -> str:
        """把已有音频文件转成 ogg（libopus 32k），返回新文件路径。"""
        ogg = Path(tempfile.mktemp(suffix=".ogg"))
        subprocess.run(
            [FFMPEG, "-y", "-i", str(src_path), "-c:a", "libopus", "-b:a", "32k", str(ogg)],
            check=True, capture_output=True,
        )
        return str(ogg)

    async def synth_ogg(self, text: str, *, emotion: str | None = None,
                        voice: str | None = None) -> Path:
        """文本 → mp3 → ogg（原 bot.py 的 tts_to_ogg）。"""
        mp3 = Path(tempfile.mktemp(suffix=".mp3"))
        await self.synth(text, mp3, emotion=emotion, voice=voice)
        ogg = Path(await self.to_ogg(mp3))
        mp3.unlink(missing_ok=True)
        return ogg

    def _speakable(self, s: str) -> bool:
        """分句碎片里要有真实文字才值得合成——纯标点（如省略号"…"被独立切出）TTS 会返回空音频。"""
        return bool(s.strip()) and bool(SPEAKABLE.search(s))

    async def safe_ogg(self, sentence: str, emotion: str, voice_hint: str = "") -> Path | None:
        """原 bot.py 的 _safe_ogg：不可说的碎片返回 None，合成失败记日志返回 None。"""
        if not self._speakable(sentence):
            return None
        try:
            return await self.synth_ogg(sentence, emotion=emotion, voice=voice_hint)
        except Exception:
            log.exception("tts failed")
            return None


def apply(ctx) -> None:
    ctx.inject("config")
    ctx.provide("tts", TTSService(ctx))
