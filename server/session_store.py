"""
会话缓存模块。

为了优化实时预览性能，原图 A 上传后会在服务端缓存其 FFT 结果，
水印 B 上传后缓存其二值化掩膜。后续调参只传参数，不再重复上传图片。

同时每个会话对 preview / preview_sign 两个接口各绑定一个 ComputeSlot，
保证快速连续调参时只处理最新一次请求。
"""

import time
import threading
import uuid
import concurrent.futures
from typing import Dict, Optional

from server.compute_slot import ComputeSlot


class SessionStore:
    """
    基于内存的会话存储。

    每个会话保存：
        - processor: FFTWatermarkProcessor 实例（含 A 的 FFT）
        - analyze_result: analyze() 的返回结果
        - watermark_info: 水印文件信息
        - preview_slot: 幅度谱预览合并计算槽位
        - preview_sign_slot: 签名预览合并计算槽位
        - updated_at: 最后访问时间戳
    """

    def __init__(self, ttl_seconds: int = 3600, max_workers: int = 4):
        self._ttl = ttl_seconds
        self._max_workers = max_workers
        self._sessions: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def create(self, processor, analyze_result: dict, watermark_info: Optional[dict] = None) -> str:
        """创建新会话，返回 session_id。"""
        session_id = uuid.uuid4().hex
        with self._lock:
            self._sessions[session_id] = {
                "processor": processor,
                "analyze_result": analyze_result,
                "watermark_info": watermark_info or {},
                "preview_slot": ComputeSlot(self._executor),
                "preview_sign_slot": ComputeSlot(self._executor),
                "updated_at": time.time(),
            }
        return session_id

    def get(self, session_id: str) -> Optional[dict]:
        """获取会话，同时刷新最后访问时间。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session["updated_at"] = time.time()
            return session

    def touch(self, session_id: str) -> bool:
        """刷新会话时间，返回是否存在的布尔值。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session["updated_at"] = time.time()
                return True
            return False

    def delete(self, session_id: str) -> bool:
        """删除会话。"""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def cleanup(self) -> int:
        """清理过期会话，返回清理数量。"""
        now = time.time()
        expired = []
        with self._lock:
            for session_id, session in self._sessions.items():
                if now - session["updated_at"] > self._ttl:
                    expired.append(session_id)
            for session_id in expired:
                del self._sessions[session_id]
        return len(expired)

    def stats(self) -> dict:
        """返回当前会话统计。"""
        with self._lock:
            return {
                "count": len(self._sessions),
                "ttl": self._ttl,
                "max_workers": self._max_workers,
            }
