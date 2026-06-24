#!/usr/bin/env python3
"""
FFT 水印工具入口文件。

启动本地 HTTP 服务，默认监听 30311 端口。
"""

import sys

from server.httpserver import main

if __name__ == "__main__":
    port = 30311
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"用法：python main.py [port]，默认 {port}")
            sys.exit(1)
    main(port)
