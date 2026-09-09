"""集中配置：Config.from_env() 收拢散落在 bot.py / discord_bot.py / ov_memory.py /
asr_seed.py / tts_seed.py / gf_tools.py 的全部环境变量。

默认值与现有代码里的默认值逐字保持一致（config.env 里很多 key 根本没配，
代码默认值才是真正的生效值）。config.env 由 run.sh source，这里只读 os.environ。
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    # 平台（bot.py / discord_bot.py）
    bot_platform: str          # BOT_PLATFORM: telegram | discord
    tg_token: str              # TG_TOKEN（discord 模式下可缺省）
    discord_token: str         # DISCORD_TOKEN（仅 discord 模式必需，故给空默认）
    # 主模型（bot.py）
    llm_base_url: str          # LLM_BASE_URL
    llm_api_key: str           # LLM_API_KEY（必需，缺失即报错，同原代码）
    llm_model: str             # LLM_MODEL
    # TTS 兜底音色 / 本地 whisper（bot.py）
    tts_voice: str             # TTS_VOICE（edge-tts 音色）
    whisper_model: str         # WHISPER_MODEL
    # 历史与记忆（bot.py / ov_memory.py）
    history_turns: int         # HISTORY_TURNS
    memory_every: int          # MEMORY_EVERY（本地提炼与 OV commit 共用）
    # 心跳（bot.py）
    heartbeat_minutes: int     # HEARTBEAT_MINUTES
    heartbeat_silence_h: float # HEARTBEAT_SILENCE_H
    # 深度路由裁判（bot.py judge_depth）
    judge_api_key: str         # JUDGE_API_KEY（空则一律 minimal）
    judge_model: str           # JUDGE_MODEL
    # 豆包语音（tts_seed.py / asr_seed.py / bot.py）
    doubao_api_key: str        # DOUBAO_API_KEY（seed-tts / seedasr 共用）
    doubao_voice: str          # DOUBAO_VOICE
    doubao_context: str        # DOUBAO_CONTEXT
    doubao_tts_model: str      # DOUBAO_TTS_MODEL
    # 云端 ASR（bot.py）
    asr_model: str             # ASR_MODEL（空串关闭方舟音频理解转写）
    asr_seed: str              # ASR_SEED（"0" 关闭 seedasr，其余值开启）
    # 冲浪循环（bot.py）
    surf_minutes: int          # SURF_MINUTES
    # 工具箱（gf_tools.py）
    tavily_api_key: str        # TAVILY_API_KEY
    # OpenViking（ov_memory.py）
    ov_url: str                # OV_URL
    ov_api_key: str            # OV_API_KEY（空则 OV 全链路跳过）
    ov_peer_id: str            # OV_PEER_ID
    ov_recall_top_k: int       # OV_RECALL_TOP_K
    ov_recall_timeout: float   # OV_RECALL_TIMEOUT
    # 插件开关（core/app.py，逗号分隔插件模块短名）
    plugins_disabled: str      # PLUGINS_DISABLED（从默认插件树剔除，不允许剔除被依赖的）
    plugins_extra: str         # PLUGINS_EXTRA（追加加载）

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            bot_platform=env.get("BOT_PLATFORM", "telegram"),
            tg_token=env.get("TG_TOKEN", ""),
            discord_token=env.get("DISCORD_TOKEN", ""),
            llm_base_url=env.get("LLM_BASE_URL", "https://api.deepseek.com"),
            llm_api_key=env["LLM_API_KEY"],
            llm_model=env.get("LLM_MODEL", "deepseek-chat"),
            tts_voice=env.get("TTS_VOICE", "zh-TW-HsiaoChenNeural"),
            whisper_model=env.get("WHISPER_MODEL", "base"),
            history_turns=int(env.get("HISTORY_TURNS", "20")),
            memory_every=int(env.get("MEMORY_EVERY", "4")),
            heartbeat_minutes=int(env.get("HEARTBEAT_MINUTES", "45")),
            heartbeat_silence_h=float(env.get("HEARTBEAT_SILENCE_H", "2")),
            judge_api_key=env.get("JUDGE_API_KEY", ""),
            judge_model=env.get("JUDGE_MODEL", "Qwen/Qwen3-8B"),
            doubao_api_key=env.get("DOUBAO_API_KEY", ""),
            doubao_voice=env.get("DOUBAO_VOICE", "zh_female_xiaohe_uranus_bigtts"),
            doubao_context=env.get("DOUBAO_CONTEXT", ""),
            doubao_tts_model=env.get("DOUBAO_TTS_MODEL", ""),
            asr_model=env.get("ASR_MODEL", "doubao-seed-2-0-mini-260428"),
            asr_seed=env.get("ASR_SEED", "1"),
            surf_minutes=int(env.get("SURF_MINUTES", "180")),
            tavily_api_key=env.get("TAVILY_API_KEY", ""),
            ov_url=env.get("OV_URL", "http://127.0.0.1:1933"),
            ov_api_key=env.get("OV_API_KEY", ""),
            ov_peer_id=env.get("OV_PEER_ID", "boyfriend"),
            ov_recall_top_k=int(env.get("OV_RECALL_TOP_K", "5")),
            ov_recall_timeout=float(env.get("OV_RECALL_TIMEOUT", "30")),
            plugins_disabled=env.get("PLUGINS_DISABLED", ""),
            plugins_extra=env.get("PLUGINS_EXTRA", ""),
        )
