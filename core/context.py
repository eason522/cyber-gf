"""Context：cordis 语义的服务容器 + 事件总线 + 插件生命周期管理。

- provide/inject：服务注册与依赖注入，inject 沿 parent 链向上查找。
- on/emit：事件总线，监听器异常记日志不中断其他监听器。
- on_dispose/create_task/load_plugin/dispose：插件与后台任务的统一回收，
  dispose 逆序执行：插件 dispose → on_dispose 回调 → cancel tasks → 清监听器。
- fork()：子上下文，inject 向父链回退，provide 只影响自己（为"千人千面"预留）。

插件模块约定：顶部可有 requires = [...]（字符串服务名）、provides = [...]
（仅文档用途），必须导出 apply(ctx)（sync/async 均可），可选 dispose(ctx)。
"""

import asyncio
import importlib
import inspect
import logging

log = logging.getLogger("cyber-gf.core")


class Context:
    def __init__(self, parent: "Context | None" = None):
        self.parent = parent
        self._services: dict[str, object] = {}
        self._listeners: dict[str, list] = {}
        self._dispose_callbacks: list = []
        self._plugin_disposers: list[tuple[str, object]] = []  # (模块名, dispose 函数)
        self._tasks: set[asyncio.Task] = set()
        self._disposed = False

    # ---- 服务 ----

    def provide(self, name: str, service, *, override: bool = False) -> None:
        if name in self._services and not override:
            raise ValueError(f"service {name!r} already provided")
        self._services[name] = service

    def inject(self, name: str):
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._services:
                return ctx._services[name]
            ctx = ctx.parent
        raise KeyError(
            f"service {name!r} not found; available: {sorted(self._all_service_names())}"
        )

    def _all_service_names(self) -> set[str]:
        names: set[str] = set()
        ctx: Context | None = self
        while ctx is not None:
            names.update(ctx._services)
            ctx = ctx.parent
        return names

    def has(self, name: str) -> bool:
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._services:
                return True
            ctx = ctx.parent
        return False

    def fork(self) -> "Context":
        return Context(self)

    # ---- 事件总线 ----

    def on(self, event: str, fn) -> callable:
        """订阅事件，返回取消订阅函数。"""
        self._listeners.setdefault(event, []).append(fn)

        def off() -> None:
            try:
                self._listeners.get(event, []).remove(fn)
            except ValueError:
                pass

        return off

    async def emit(self, event: str, *args, **kw) -> None:
        listeners = list(self._listeners.get(event, []))
        if not listeners:
            return

        async def _run(fn) -> None:
            try:
                r = fn(*args, **kw)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("listener for event %r failed", event)

        await asyncio.gather(*(_run(fn) for fn in listeners))

    # ---- 生命周期 ----

    def on_dispose(self, fn):
        """注册清理回调（dispose 时逆序执行，支持 async），返回该回调。"""
        self._dispose_callbacks.append(fn)
        return fn

    def create_task(self, coro) -> asyncio.Task:
        """登记后台任务，dispose 时统一 cancel。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def load_plugin(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        requires = getattr(module, "requires", []) or []
        missing = [r for r in requires if not self.has(r)]
        if missing:
            raise RuntimeError(
                f"plugin {module_name!r} missing required services: {missing}; "
                f"available: {sorted(self._all_service_names())}"
            )
        apply = getattr(module, "apply", None)
        if apply is None:
            raise TypeError(f"plugin {module_name!r} must export apply(ctx)")
        r = apply(self)
        if inspect.isawaitable(r):
            await r
        dispose = getattr(module, "dispose", None)
        if dispose is not None:
            self._plugin_disposers.append((module_name, dispose))
        log.info("plugin loaded: %s", module_name)

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for name, fn in reversed(self._plugin_disposers):
            try:
                r = fn(self)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("dispose of plugin %s failed", name)
        self._plugin_disposers.clear()
        for fn in reversed(self._dispose_callbacks):
            try:
                r = fn()
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("on_dispose callback failed")
        self._dispose_callbacks.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._listeners.clear()
