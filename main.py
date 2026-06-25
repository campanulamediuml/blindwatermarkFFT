#!/usr/bin/env python3
"""
FFT 水印工具入口文件。

启动本地 HTTP 服务，默认监听 30311 端口。

用法：
    python main.py
    python main.py --port 30313
    python main.py --port 30313 --log-level debug
"""

from server.httpserver import main

if __name__ == "__main__":
    main()
