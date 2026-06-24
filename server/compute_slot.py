"""
计算槽位：合并同一 session 同一接口的连续请求。

规则：
    - 同一时刻只执行一个计算任务
    - 如果正在计算时收到新请求，只保留最新的 pending 参数
    - 当前任务完成后（含结果打包/返回），若存在 pending 则立即执行最新任务
    - 所有等待中的请求都会收到最新一次计算的结果
"""

import threading
import asyncio
import concurrent.futures
from typing import Any, Callable, Optional


class ComputeSlot:
    """
    单个合并计算槽位。

    用法：
        slot = ComputeSlot(executor)
        result = await slot.call(args, compute_fn)
    """

    def __init__(self, executor: concurrent.futures.Executor):
        self._executor = executor
        self._loop = asyncio.get_event_loop()
        self._lock = threading.Lock()
        self._running = False
        self._pending_args: Optional[Any] = None
        self._pending_futures: list = []
        self._last_fn: Optional[Callable[[Any], Any]] = None

    async def call(self, args: Any, fn: Callable[[Any], Any]) -> Any:
        """
        提交一次计算请求。

        如果槽位空闲，立即执行；如果正在计算，替换 pending 参数并等待最新结果。
        """
        my_future = self._loop.create_future()

        with self._lock:
            if not self._running:
                self._running = True
                self._pending_args = None
                self._pending_futures = []
                self._last_fn = fn
                self._dispatch(args, fn, [my_future])
            else:
                # 正在计算：用最新参数替换 pending，本 future 等待下一轮结果
                self._pending_args = args
                self._pending_futures.append(my_future)

        return await my_future

    def _dispatch(self, args: Any, fn: Callable[[Any], Any], futures: list):
        """在线程池中启动计算任务。"""
        def task():
            try:
                result = fn(args)
                self._loop.call_soon_threadsafe(self._on_done, result, None, futures)
            except Exception as e:
                self._loop.call_soon_threadsafe(self._on_done, None, e, futures)

        self._executor.submit(task)

    def _on_done(self, result: Any, error: Optional[Exception], completed_futures: list):
        """计算完成回调（在事件循环线程中执行）。"""
        with self._lock:
            pending_args = self._pending_args
            pending_futures = self._pending_futures
            self._pending_args = None
            self._pending_futures = []

            if pending_args is not None:
                # 有更新的请求在等待，立即用最新参数启动下一次计算
                self._dispatch(pending_args, self._last_fn, pending_futures)
            else:
                self._running = False

        # 通知本次已完成的 futures（必须在持有锁之外设置，避免回调中死锁）
        for f in completed_futures:
            if f.done():
                continue
            if error is not None:
                f.set_exception(error)
            else:
                f.set_result(result)
