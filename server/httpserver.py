"""
Tornado HTTP 服务层。

只负责接收 HTTP 请求、解析参数、调用 image_processor 中的图像处理类，
并将结果返回给前端。不包含任何图像处理算法细节。
"""

import os
import io
import json
import signal
import time
import base64
import uuid
import argparse
import logging
from typing import Dict, Tuple

import psutil

import tornado.ioloop
import tornado.web
import tornado.httpserver
from tornado.log import enable_pretty_logging

from server.image_processor import FFTWatermarkProcessor, _make_thumbnail
from server.logger import AppLogger
from server.session_store import SessionStore


# 会话存储实例
session_store = SessionStore(ttl_seconds=3600)


def _build_multipart_response(parts: Dict[str, bytes], boundary: str = None) -> Tuple[str, bytes]:
    """
    构造 multipart/form-data 响应体。

    Args:
        parts: {field_name: png_bytes}

    Returns:
        (content_type, body)
    """
    if boundary is None:
        boundary = f"----FormBoundary{uuid.uuid4().hex}"
    body = io.BytesIO()
    for name, data in parts.items():
        body.write(f"--{boundary}\r\n".encode("ascii"))
        body.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{name}.png"\r\n'.encode("ascii")
        )
        body.write(b"Content-Type: image/png\r\n\r\n")
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", body.getvalue()


# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")


class MainHandler(tornado.web.RequestHandler):
    """主页：返回 index.html"""

    def get(self):
        index_path = os.path.join(STATIC_DIR, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            self.write(f.read())
        self.set_header("Content-Type", "text/html; charset=utf-8")


class StaticFileHandler(tornado.web.RequestHandler):
    """静态文件服务（JS/CSS 等）。"""

    def get(self, path):
        file_path = os.path.join(STATIC_DIR, path)
        if not os.path.abspath(file_path).startswith(os.path.abspath(STATIC_DIR)):
            raise tornado.web.HTTPError(403)
        if not os.path.isfile(file_path):
            raise tornado.web.HTTPError(404)

        # 根据后缀设置 Content-Type
        ext = os.path.splitext(file_path)[1].lower()
        content_type_map = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".html": "text/html; charset=utf-8",
        }
        content_type = content_type_map.get(ext, "application/octet-stream")

        with open(file_path, "rb") as f:
            self.write(f.read())
        self.set_header("Content-Type", content_type)


class AnalyzeHandler(tornado.web.RequestHandler):
    """POST /api/analyze：分析原图，返回 3×3 所需的 9 张图。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}

        try:
            file_list = self.request.files.get("image")
            if not file_list:
                raise ValueError("缺少原图文件")

            file_info = {"image": f"{file_list[0]['filename']}({len(file_list[0]['body'])} bytes)"}
            image_bytes = file_list[0]["body"]
            processor = FFTWatermarkProcessor(image_bytes)
            result = processor.analyze()

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps(result))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/analyze",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                error=error_msg,
            )


class PreviewHandler(tornado.web.RequestHandler):
    """POST /api/preview：预览水印叠加到幅度谱后的效果。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}
        args = {}

        try:
            image_files = self.request.files.get("image")
            watermark_files = self.request.files.get("watermark")
            if not image_files or not watermark_files:
                raise ValueError("缺少原图或水印文件")

            image_bytes = image_files[0]["body"]
            watermark_bytes = watermark_files[0]["body"]
            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))

            file_info = {
                "image": f"{image_files[0]['filename']}({len(image_bytes)} bytes)",
                "watermark": f"{watermark_files[0]['filename']}({len(watermark_bytes)} bytes)",
            }
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = FFTWatermarkProcessor(image_bytes)
            result = processor.preview_watermark(watermark_bytes, scale_frontend, power_frontend, freq_frontend)

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps(result))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/preview",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                args=args,
                error=error_msg,
            )


class PreviewSignHandler(tornado.web.RequestHandler):
    """POST /api/preview_sign：实时预览签名后的图像（返回 base64 数据 URL）。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}
        args = {}

        try:
            image_files = self.request.files.get("image")
            watermark_files = self.request.files.get("watermark")
            if not image_files or not watermark_files:
                raise ValueError("缺少原图或水印文件")

            image_bytes = image_files[0]["body"]
            watermark_bytes = watermark_files[0]["body"]
            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))

            file_info = {
                "image": f"{image_files[0]['filename']}({len(image_bytes)} bytes)",
                "watermark": f"{watermark_files[0]['filename']}({len(watermark_bytes)} bytes)",
            }
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = FFTWatermarkProcessor(image_bytes)
            result_bytes = processor.sign(watermark_bytes, scale_frontend, power_frontend, freq_frontend)

            b64 = base64.b64encode(result_bytes).decode("ascii")
            data_url = f"data:image/png;base64,{b64}"

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"signed": data_url}))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/preview_sign",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                args=args,
                error=error_msg,
            )


class SignHandler(tornado.web.RequestHandler):
    """POST /api/sign：执行水印签名，返回带水印图像字节流。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}
        args = {}

        try:
            image_files = self.request.files.get("image")
            watermark_files = self.request.files.get("watermark")
            if not image_files or not watermark_files:
                raise ValueError("缺少原图或水印文件")

            image_bytes = image_files[0]["body"]
            watermark_bytes = watermark_files[0]["body"]
            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))

            file_info = {
                "image": f"{image_files[0]['filename']}({len(image_bytes)} bytes)",
                "watermark": f"{watermark_files[0]['filename']}({len(watermark_bytes)} bytes)",
            }
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = FFTWatermarkProcessor(image_bytes)
            result_bytes = processor.sign(watermark_bytes, scale_frontend, power_frontend, freq_frontend)

            self.set_header("Content-Type", "image/png")
            self.set_header("Content-Disposition", "attachment; filename=signed.png")
            self.write(result_bytes)
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/sign",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                args=args,
                error=error_msg,
            )


# ==================== 高性能会话接口 ====================
# 以下接口用于优化实时预览：A 和 B 上传一次后缓存在服务端，
# 后续调参只传 session_id 和参数，返回二进制图像而非 base64 JSON。


class SessionUploadHandler(tornado.web.RequestHandler):
    """POST /api/session/upload：上传原图 A（可选水印 B），创建会话。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}
        result = {}

        try:
            image_files = self.request.files.get("image")
            if not image_files:
                raise ValueError("缺少原图文件")

            image_bytes = image_files[0]["body"]
            file_info["image"] = f"{image_files[0]['filename']}({len(image_bytes)} bytes)"

            processor = FFTWatermarkProcessor(image_bytes)
            analyze_result = processor.analyze()

            watermark_info = {}
            watermark_files = self.request.files.get("watermark")
            if watermark_files:
                watermark_bytes = watermark_files[0]["body"]
                file_info["watermark"] = f"{watermark_files[0]['filename']}({len(watermark_bytes)} bytes)"
                processor.set_watermark(watermark_bytes)
                watermark_info = file_info.get("watermark", "")

            # 生成原图缩略图，用于任务列表展示
            thumbnail_original = _make_thumbnail(image_bytes, max_size=120)

            image_info = file_info.get("image", "")
            session_id = session_store.create(
                processor,
                analyze_result,
                image_info=image_info,
                watermark_info=watermark_info,
                thumbnail_original=thumbnail_original,
            )

            # 如果有水印，生成默认参数的签名预览缩略图
            thumbnail_signed = None
            if watermark_files:
                sign_bytes = processor.sign(
                    scale_frontend=50, power_frontend=5, freq_frontend=10
                )
                thumbnail_signed = _make_thumbnail(sign_bytes, max_size=120)
                session_store.update_thumbnail_signed(session_id, thumbnail_signed)

            result = {
                "session_id": session_id,
                "analyze": analyze_result,
                "thumbnail_original": thumbnail_original,
                "thumbnail_signed": thumbnail_signed,
            }

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps(result))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/upload",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                error=error_msg,
            )


class SessionWatermarkHandler(tornado.web.RequestHandler):
    """POST /api/session/watermark：为已有会话上传/更新水印 B。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        file_info = {}

        try:
            session_id = self.get_body_argument("session_id", "")
            session = session_store.get(session_id)
            if session is None:
                raise ValueError("会话不存在或已过期")

            watermark_files = self.request.files.get("watermark")
            if not watermark_files:
                raise ValueError("缺少水印文件")

            watermark_bytes = watermark_files[0]["body"]
            file_info["watermark"] = f"{watermark_files[0]['filename']}({len(watermark_bytes)} bytes)"

            processor = session["processor"]
            processor.set_watermark(watermark_bytes)
            session["watermark_info"] = file_info["watermark"]

            # 生成默认参数的签名预览缩略图
            sign_bytes = processor.sign(
                scale_frontend=50, power_frontend=5, freq_frontend=10
            )
            thumbnail_signed = _make_thumbnail(sign_bytes, max_size=120)
            session_store.update_thumbnail_signed(session_id, thumbnail_signed)

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"success": True, "thumbnail_signed": thumbnail_signed}))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/watermark",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                files=file_info,
                args={"session_id": session_id} if 'session_id' in locals() else {},
                error=error_msg,
            )


class SessionPreviewHandler(tornado.web.RequestHandler):
    """POST /api/session/preview：只传参数，返回 3 张幅度谱 PNG（multipart）。"""

    async def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        args = {}

        try:
            session_id = self.get_body_argument("session_id", "")
            session = session_store.get(session_id)
            if session is None:
                raise ValueError("会话不存在或已过期")

            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = session["processor"]

            def compute(_args):
                return processor.preview_watermark_bytes(
                    scale_frontend=_args["scale"],
                    power_frontend=_args["power"],
                    freq_frontend=_args["freq"],
                )

            parts = await session["preview_slot"].call(args, compute)

            # 缓存当前参数
            try:
                session_store.update_params(session_id, args)
            except Exception:
                pass

            parts = {
                "amplitude_r": parts["r"],
                "amplitude_g": parts["g"],
                "amplitude_b": parts["b"],
            }
            content_type, body = _build_multipart_response(parts)

            self.set_header("Content-Type", content_type)
            self.write(body)
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/preview",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                args=args,
                error=error_msg,
            )


class SessionPreviewSignHandler(tornado.web.RequestHandler):
    """POST /api/session/preview_sign：只传参数，返回签名预览 PNG 二进制。"""

    async def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        args = {}

        try:
            session_id = self.get_body_argument("session_id", "")
            session = session_store.get(session_id)
            if session is None:
                raise ValueError("会话不存在或已过期")

            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = session["processor"]

            def compute(_args):
                return processor.sign(
                    scale_frontend=_args["scale"],
                    power_frontend=_args["power"],
                    freq_frontend=_args["freq"],
                )

            result_bytes = await session["preview_sign_slot"].call(args, compute)

            # 缓存签名预览缩略图和当前参数
            try:
                thumbnail_signed = _make_thumbnail(result_bytes, max_size=120)
                session_store.update_thumbnail_signed(session_id, thumbnail_signed)
                session_store.update_params(session_id, args)
            except Exception:
                # 缩略图生成失败不影响主流程
                pass

            self.set_header("Content-Type", "image/png")
            self.write(result_bytes)
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/preview_sign",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                args=args,
                error=error_msg,
            )


class SessionListHandler(tornado.web.RequestHandler):
    """GET /api/session/list：列出所有会话摘要。"""

    def get(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None

        try:
            sessions = session_store.list()
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"sessions": sessions}))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/list",
                method="GET",
                status_code=status,
                elapsed_ms=elapsed_ms,
                error=error_msg,
            )


class SessionInfoHandler(tornado.web.RequestHandler):
    """GET /api/session/info?session_id=xxx：获取指定会话的完整状态，用于切换任务。"""

    def get(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        args = {}

        try:
            session_id = self.get_argument("session_id", "")
            args = {"session_id": session_id}
            session = session_store.get(session_id)
            if session is None:
                raise ValueError("会话不存在或已过期")

            result = {
                "session_id": session_id,
                "analyze": session["analyze_result"],
                "thumbnail_original": session.get("thumbnail_original"),
                "thumbnail_signed": session.get("thumbnail_signed"),
                "image_info": session.get("image_info", {}),
                "watermark_info": session.get("watermark_info", {}),
                "params": session.get("params", {"scale": 50, "power": 5, "freq": 10}),
            }

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps(result))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/info",
                method="GET",
                status_code=status,
                elapsed_ms=elapsed_ms,
                args=args,
                error=error_msg,
            )


class SessionDeleteHandler(tornado.web.RequestHandler):
    """POST /api/session/delete：删除指定会话。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        args = {}

        try:
            session_id = self.get_body_argument("session_id", "")
            args = {"session_id": session_id}
            success = session_store.delete(session_id)
            if not success:
                raise ValueError("会话不存在或已过期")

            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"success": True}))
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/delete",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                args=args,
                error=error_msg,
            )


class SessionSignHandler(tornado.web.RequestHandler):
    """POST /api/session/sign：只传参数，返回带水印图像 PNG 字节流。"""

    def post(self):
        logger = AppLogger()
        start = time.time()
        status = 200
        error_msg = None
        args = {}

        try:
            session_id = self.get_body_argument("session_id", "")
            session = session_store.get(session_id)
            if session is None:
                raise ValueError("会话不存在或已过期")

            scale_frontend = float(self.get_body_argument("scale", "50"))
            power_frontend = float(self.get_body_argument("power", "5"))
            freq_frontend = float(self.get_body_argument("freq", "10"))
            args = {"scale": scale_frontend, "power": power_frontend, "freq": freq_frontend}

            processor = session["processor"]
            result_bytes = processor.sign(
                scale_frontend=scale_frontend,
                power_frontend=power_frontend,
                freq_frontend=freq_frontend,
            )

            # 缓存签名缩略图和当前参数
            try:
                thumbnail_signed = _make_thumbnail(result_bytes, max_size=120)
                session_store.update_thumbnail_signed(session_id, thumbnail_signed)
                session_store.update_params(session_id, args)
            except Exception:
                pass

            self.set_header("Content-Type", "image/png")
            self.set_header("Content-Disposition", "attachment; filename=signed.png")
            self.write(result_bytes)
        except Exception as e:
            status = 400
            error_msg = str(e)
            self.set_status(status)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"error": error_msg}))
        finally:
            elapsed_ms = (time.time() - start) * 1000
            logger.log_request(
                url="/api/session/sign",
                method="POST",
                status_code=status,
                elapsed_ms=elapsed_ms,
                args=args,
                error=error_msg,
            )


def make_app() -> tornado.web.Application:
    """构造 Tornado Application。"""
    return tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/api/analyze", AnalyzeHandler),
            (r"/api/preview", PreviewHandler),
            (r"/api/preview_sign", PreviewSignHandler),
            (r"/api/sign", SignHandler),
            # 高性能会话接口
            (r"/api/session/upload", SessionUploadHandler),
            (r"/api/session/watermark", SessionWatermarkHandler),
            (r"/api/session/preview", SessionPreviewHandler),
            (r"/api/session/preview_sign", SessionPreviewSignHandler),
            (r"/api/session/sign", SessionSignHandler),
            # 多任务管理接口
            (r"/api/session/list", SessionListHandler),
            (r"/api/session/info", SessionInfoHandler),
            (r"/api/session/delete", SessionDeleteHandler),
            (r"/static/(.*)", StaticFileHandler),
        ],
        static_path=None,  # 我们自己处理静态文件
        debug=True,
    )


def main():
    """启动 HTTP 服务。支持命令行参数：--port、--log-level 等。"""
    parser = argparse.ArgumentParser(description="FFT 水印工具 HTTP 服务")
    parser.add_argument(
        "--port",
        type=int,
        default=30311,
        help="监听端口（默认 30311）",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="日志级别（默认 info）",
    )
    args = parser.parse_args()

    # 配置根日志级别
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # 启用 Tornado 的彩色访问日志，每个 HTTP 请求都会打印
    enable_pretty_logging()

    app = make_app()

    # 显式创建 HTTPServer，方便在收到退出信号时优雅关闭
    server = tornado.httpserver.HTTPServer(app)
    server.listen(args.port)

    io_loop = tornado.ioloop.IOLoop.current()

    def shutdown(signum, frame):
        """收到 SIGINT/SIGTERM 后优雅退出。"""
        print("\nReceived shutdown signal, stopping server...")
        # 停止接收新连接
        server.stop()
        # 在 IOLoop 中调度停止事件循环
        io_loop.add_callback_from_signal(io_loop.stop)

    # 注册信号处理函数
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 启动会话过期清理定时器（每 10 分钟一次）
    def _cleanup_sessions():
        count = session_store.cleanup()
        if count:
            print(f"[SessionStore] cleaned up {count} expired sessions")
    cleanup_callback = tornado.ioloop.PeriodicCallback(_cleanup_sessions, 1000)
    cleanup_callback.start()

    # 启动内存监控定时器（每 10 秒打印一次当前进程内存占用，单位 MB）
    def _log_memory():
        pid = os.getpid()
        proc = psutil.Process(pid)
        mem = proc.memory_info()
        print(
            f"[Memory] PID {pid} | RSS {mem.rss / 1024 / 1024:.2f} MB | "
            f"VMS {mem.vms / 1024 / 1024:.2f} MB"
        )

    memory_callback = tornado.ioloop.PeriodicCallback(_log_memory, 6000)
    memory_callback.start()

    print(f"FFT Watermark server is running at http://localhost:{args.port}/")
    print("Press Ctrl+C to stop.")
    io_loop.start()
    print("Server stopped.")


if __name__ == "__main__":
    main()
