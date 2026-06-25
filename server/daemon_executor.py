"""
创建 daemon 线程池的工具模块。

所有可能阻塞服务退出的后台计算线程都必须是 daemon，
确保用户按 Ctrl+C 时主进程能立即终止。
"""

import threading
from concurrent.futures import ThreadPoolExecutor


def create_daemon_executor(max_workers=1) -> ThreadPoolExecutor:
    """
    创建一个所有工作线程都是 daemon 的 ThreadPoolExecutor。

    在创建期间临时 patch threading.Thread.__init__，使得线程池创建的
    工作线程默认 daemon=True。创建完成后立即恢复原始实现，避免影响
    其他线程。
    """
    original_init = threading.Thread.__init__

    def _daemon_init(self, *args, **kwargs):
        kwargs["daemon"] = True
        original_init(self, *args, **kwargs)

    threading.Thread.__init__ = _daemon_init
    try:
        executor = ThreadPoolExecutor(max_workers=max_workers)
        # 提交一个空任务触发线程创建，确保线程在 patch 期间被创建
        executor.submit(lambda: None)
    finally:
        threading.Thread.__init__ = original_init

    return executor
