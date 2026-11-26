"""命令行入口：``python -m dns_lab``。

启动 Web 查询台（默认 http://127.0.0.1:8080）与真实 UDP DNS 前端
（默认 127.0.0.1:5354，可用 dig 直接查询）。
"""

from __future__ import annotations

import argparse
import signal
import time

from .lab import build_default_lab
from .udp_frontend import UdpDnsFrontend
from .web import WebConsole


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 DNS 缓存解析器实验台")
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--dns-host", default="127.0.0.1")
    parser.add_argument("--dns-port", type=int, default=5354)
    parser.add_argument("--no-udp", action="store_true", help="不启动 UDP DNS 前端")
    parser.add_argument("--stale-window", type=int, default=300,
                        help="serve-stale 宽限秒数（默认 300）")
    parser.add_argument("--failure-ttl", type=int, default=5,
                        help="SERVFAIL/传输故障缓存秒数（默认 5）")
    args = parser.parse_args()

    lab = build_default_lab(stale_window=args.stale_window,
                            failure_ttl=args.failure_ttl)
    web = WebConsole(lab, args.web_host, args.web_port)
    web.start()

    udp = None
    if not args.no_udp:
        udp = UdpDnsFrontend(lab["resolver"], args.dns_host, args.dns_port)
        udp.start()

    print("=" * 64)
    print(" 本地 DNS 缓存解析器实验台已启动")
    print(f"  查询台 Web UI : {web.address}")
    if udp is not None:
        print(f"  UDP DNS 前端 : {args.dns_host}:{args.dns_port}")
        print(f"    示例: dig @{args.dns_host} -p {args.dns_port} www.example.com")
    print(" 按 Ctrl+C 退出")
    print("=" * 64)

    stop = False

    def _handle_sigint(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        while not stop:
            time.sleep(0.5)
    finally:
        if udp is not None:
            udp.stop()
        web.stop()
        print("\n已停止。")


if __name__ == "__main__":
    main()
