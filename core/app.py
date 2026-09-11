"""core/app.py：插件架构入口。

load config → 建 root Context → 按依赖序加载插件树 → emit "ready"（平台插件此时才
启动轮询/连接）→ 阻塞等待；KeyboardInterrupt/CancelledError 时 ctx.dispose() 干净退出
（插件 dispose → on_dispose（平台 shutdown）→ cancel 后台任务）。

插件树由 DEFAULT_PLUGINS + 平台插件构成；PLUGINS_DISABLED 剔除、PLUGINS_EXTRA 追加
（逗号分隔模块短名，如 PLUGINS_EXTRA=plugins.foo 的短名 foo）。
"""

import asyncio
import importlib
import logging
import logging.handlers
from pathlib import Path

from core.config import Config
from core.context import Context

BASE_DIR = Path(__file__).resolve().parent.parent

# 默认插件树（按依赖序），平台插件按 BOT_PLATFORM 追加在末尾
DEFAULT_PLUGINS = [
    "llm", "persona", "sessions", "memory_local", "memory_openviking",
    "asr", "tts", "tools_builtin", "depth_router", "interests", "memory_md", "scheduler",
    "dopamine", "social", "chat",
    "heartbeat", "surf",
]
PLATFORM_PLUGINS = {"telegram": "platform_telegram", "discord": "platform_discord"}


def _setup_logging() -> None:
    """文件按天轮转保留 14 天（重启不丢），同时输出到 stdout（systemd journal 收）。"""
    if logging.root.handlers:
        return
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_h = logging.handlers.TimedRotatingFileHandler(
        log_dir / "cyber-gf.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    logging.root.setLevel(logging.INFO)
    logging.root.addHandler(file_h)
    logging.root.addHandler(stream_h)


def resolve_plugins(cfg: Config) -> list[str]:
    """默认插件树 + 平台插件，应用 PLUGINS_DISABLED / PLUGINS_EXTRA，并静态校验依赖。"""
    platform = PLATFORM_PLUGINS.get(cfg.bot_platform)
    if platform is None:
        raise ValueError(f"unknown BOT_PLATFORM: {cfg.bot_platform!r}（可选：{sorted(PLATFORM_PLUGINS)}）")
    plugins = DEFAULT_PLUGINS + [platform]
    for name in (s.strip() for s in cfg.plugins_disabled.split(",")):
        if not name:
            continue
        if name not in plugins:
            raise ValueError(f"PLUGINS_DISABLED: {name!r} 不在默认插件树中：{plugins}")
        plugins.remove(name)
    for name in (s.strip() for s in cfg.plugins_extra.split(",")):
        if name and name not in plugins:
            plugins.append(name)
    # 静态依赖校验：剔除/追加后每个插件的 requires 都必须有提供者（报清晰错误）
    available = {"config"}
    for name in plugins:
        module = importlib.import_module(f"plugins.{name}")
        requires = getattr(module, "requires", []) or []
        missing = [r for r in requires if r not in available]
        if missing:
            raise RuntimeError(
                f"插件 {name!r} 缺少依赖服务 {missing}；请检查 PLUGINS_DISABLED 是否剔除了提供者"
            )
        available.update(getattr(module, "provides", []) or [])
    return plugins


async def amain() -> None:
    cfg = Config.from_env()
    ctx = Context()
    ctx.provide("config", cfg)
    plugins = resolve_plugins(cfg)
    for name in plugins:
        await ctx.load_plugin(f"plugins.{name}")
    log = logging.getLogger("cyber-gf")
    log.info("plugins loaded: %s", plugins)
    await ctx.emit("ready")  # 平台插件在此刻启动轮询/连接
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await ctx.dispose()


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
