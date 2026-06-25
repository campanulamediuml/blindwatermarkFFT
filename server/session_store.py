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
from typing import Dict, List, Optional

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
        - created_at: 创建时间戳
        - thumbnail_original: 原图缩略图 data URL
        - thumbnail_signed: 签名预览缩略图 data URL
        - params: 当前参数 {scale, power, freq}
    """

    def __init__(self, ttl_seconds: int = 3600, max_workers: int = 4):
        self._ttl = ttl_seconds
        self._max_workers = max_workers
        self._sessions: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def create(
        self,
        processor,
        analyze_result: dict,
        image_info: Optional[dict] = None,
        watermark_info: Optional[dict] = None,
        thumbnail_original: Optional[str] = None,
    ) -> str:
        """创建新会话，返回 session_id。任务最多保留 8 个，超出时删除最老的任务。"""
        session_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            # 任务数量上限为 8，超出时按创建时间删除最老的任务
            max_sessions = 8
            while len(self._sessions) >= max_sessions:
                oldest_id = min(
                    self._sessions.keys(),
                    key=lambda sid: self._sessions[sid]["created_at"],
                )
                oldest_session = self._sessions.pop(oldest_id)
                try:
                    oldest_session["processor"].close()
                except Exception:
                    pass

            self._sessions[session_id] = {
                "processor": processor,
                "analyze_result": analyze_result,
                "image_info": image_info or {},
                "watermark_info": watermark_info or {},
                "preview_slot": ComputeSlot(self._executor),
                "preview_sign_slot": ComputeSlot(self._executor),
                "updated_at": now,
                "created_at": now,
                "thumbnail_original": thumbnail_original,
                "thumbnail_signed": None,
                "params": {
                    "scale": 50,
                    "power": 5,
                    "freq": 10,
                },
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
        """删除会话，并释放处理器资源。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            try:
                session["processor"].close()
            except Exception:
                pass
            return True

    def list(self) -> List[dict]:
        """返回所有会话的摘要列表（只包含可展示给前端的信息）。"""
        now = time.time()
        result = []
        with self._lock:
            for session_id, session in self._sessions.items():
                result.append({
                    "session_id": session_id,
                    "thumbnail_original": session.get("thumbnail_original"),
                    "thumbnail_signed": session.get("thumbnail_signed"),
                    "image_info": session.get("image_info", {}),
                "watermark_info": session.get("watermark_info", {}),
                    "params": session.get("params", {"scale": 50, "power": 5, "freq": 10}),
                    "created_at": session.get("created_at", 0),
                    "updated_at": session.get("updated_at", 0),
                    "expired_in": max(0, self._ttl - (now - session["updated_at"])),
                })
        # 按创建时间倒序，最新的在前面
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    def update_thumbnail_original(self, session_id: str, thumbnail_original: str) -> bool:
        """更新会话的原图缩略图。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["thumbnail_original"] = thumbnail_original
            session["updated_at"] = time.time()
            return True

    def update_thumbnail_signed(self, session_id: str, thumbnail_signed: str) -> bool:
        """更新会话的签名预览缩略图。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["thumbnail_signed"] = thumbnail_signed
            session["updated_at"] = time.time()
            return True

    def update_params(self, session_id: str, params: dict) -> bool:
        """更新会话的当前参数。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["params"].update(params)
            session["updated_at"] = time.time()
            return True

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
