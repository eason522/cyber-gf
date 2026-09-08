"""临时测试：用官方示例指令+文本直出 3 段音频（不走 SOUL/记忆），验证 additions.context_texts 效果。

用法：set -a && source config.env && .venv/bin/python test_tts_official.py
产物：/tmp/tts_test_{悄悄话,生气,哭腔}.mp3
"""
import asyncio
from pathlib import Path

import tts_seed

CASES = [
    ("悄悄话", "用asmr的语气来试试撩撩我",
     "当然可以啦，每次听到你的声音，我都觉得心里暖暖的。"),
    ("生气", "你得跟我互怼！就是跟我用吵架的语气对话",
     "那你另请高明啊，你找我干嘛！我告诉你，你也不是什么好东西！"),
    ("哭腔", "用颤抖沙哑、带着崩溃与绝望的哭腔，夹杂着质问与心碎的语气说",
     "我逆转时空九十九次救你，你却次次死于同一支暗箭。谢珩，原来不是天要亡你……是你宁死也不肯为我活下去。"),
]


async def main():
    for name, instruction, text in CASES:
        out = Path(f"/tmp/tts_test_{name}.mp3")
        await tts_seed.synth(text, out, context=[instruction])
        print(f"{name}: {out} {out.stat().st_size} bytes")


asyncio.run(main())
