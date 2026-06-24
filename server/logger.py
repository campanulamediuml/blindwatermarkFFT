"""
日志模块。

按日期和时间组织日志文件：
    misc/log/YYYY/MM/DD/HH-{PID}.log

记录内容：
    - 时间戳
    - 请求 URL
    - 请求参数（alpha 等）
    - 上传文件名及大小
    - 处理耗时
    - 响应状态
    - 错误信息
"""

import os
import time
import threading
from datetime import datetime
from typing import Dict, Any, Optional


# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 日志根目录
LOG_ROOT = os.path.join(PROJECT_ROOT, "misc", "log")

# 日志级别
LEVEL_INFO = "INFO"
LEVEL_ERROR = "ERROR"
LEVEL_WARNING = "WARNING"


class AppLogger:
    """
    应用日志记录器。

    按进程单例使用，每个进程写入独立的日志文件（按小时 + PID 区分）。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._pid = os.getpid()
                    cls._instance._file_path = None
                    cls._instance._file_handle = None
                    cls._instance._ensure_file()
        return cls._instance

    def _ensure_file(self):
        """确保日志目录和文件已创建。"""
        now = datetime.now()
        log_dir = os.path.join(
            LOG_ROOT,
            f"{now.year:04d}",
            f"{now.month:02d}",
            f"{now.day:02d}",
        )
        os.makedirs(log_dir, exist_ok=True)

        file_path = os.path.join(log_dir, f"{now.hour:02d}-{self._pid}.log")

        # 如果跨小时，需要切换文件
        if self._file_path != file_path:
            if self._file_handle:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
            self._file_path = file_path
            self._file_handle = open(file_path, "a", encoding="utf-8")

    def _write(self, level: str, message: str, extra: Optional[Dict[str, Any]] = None):
        """写入一条日志。"""
        self._ensure_file()

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        extra_str = ""
        if extra:
            parts = []
            for key, value in extra.items():
                parts.append(f"{key}={value}")
            extra_str = " | " + " | ".join(parts)

        line = f"[{timestamp}] [{level}] {message}{extra_str}\n"
        self._file_handle.write(line)
        self._file_handle.flush()

    def info(self, message: str, extra: Optional[Dict[str, Any]] = None):
        """记录 INFO 级别日志。"""
        self._write(LEVEL_INFO, message, extra)

    def error(self, message: str, extra: Optional[Dict[str, Any]] = None):
        """记录 ERROR 级别日志。"""
        self._write(LEVEL_ERROR, message, extra)

    def warning(self, message: str, extra: Optional[Dict[str, Any]] = None):
        """记录 WARNING 级别日志。"""
        self._write(LEVEL_WARNING, message, extra)

    def log_request(
        self,
        url: str,
        method: str,
        status_code: int,
        elapsed_ms: float,
        files: Optional[Dict[str, Any]] = None,
        args: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        """
        记录 HTTP 请求日志。

        Args:
            url: 请求 URL
            method: HTTP 方法
            status_code: 响应状态码
            elapsed_ms: 处理耗时（毫秒）
            files: 上传文件信息，格式 {field_name: file_name(size_bytes)}
            args: 请求参数，如 {alpha: 50}
            error: 错误信息（如果有）
        """
        extra = {
            "method": method,
            "status": status_code,
            "elapsed_ms": f"{elapsed_ms:.2f}",
        }
        if files:
            extra["files"] = files
        if args:
            extra["args"] = args
        if error:
            extra["error"] = error

        level = LEVEL_ERROR if status_code >= 400 or error else LEVEL_INFO
        self._write(level, f"{method} {url}", extra)

    def close(self):
        """关闭日志文件句柄。"""
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None


# 模块级便捷函数
def get_logger() -> AppLogger:
    """获取日志记录器实例。"""
    return AppLogger()
