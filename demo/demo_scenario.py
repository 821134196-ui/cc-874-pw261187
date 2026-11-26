"""脚本化演示：通过查询台 HTTP API 跑完整缓存生命周期。

用法：
    python -m dns_lab --web-port 8080 &
    python demo/demo_scenario.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("DNS_LAB_URL", "http://127.0.0.1:8080")
NS = ["ns1.example.net", "ns2.example.net", "ns3.example.net"]


def call(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def heading(text: str) -> None:
    print("\n" + "=" * 68 + f"\n {text}\n" + "=" * 68)


def show(result: dict) -> None:
    print(f"  rcode={result['rcode_name']:<9} 出处={result['origin_label']}")
    print(f"  source={result['source']}  回源={result['fetched']}")
    if result["attempts"]:
        print(f"  尝试链: {' -> '.join(result['attempts'])}")
    for ans in result["answers"]:
        print(f"  应答: {ans['name']} {ans['rtype']} {ans['data']} TTL={ans['ttl']}s")
    if result["note"]:
        print(f"  说明: {result['note']}")


def main() -> int:
    try:
        call("/api/state")
    except Exception as exc:
        print(f"无法连接查询台 {BASE}：{exc}\n请先运行 python -m dns_lab")
        return 1

    heading("0. 重置环境并冻结受控时钟")
    call("/api/cache/clear", {})
    call("/api/clock/reset", {})
    call("/api/clock/freeze", {})
    for name in NS:
        call("/api/authority/mode", {"server": name, "mode": "normal"})

    heading("1. 正向缓存：首次回源，二次零回源")
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])

    heading("2. 否定缓存：NXDOMAIN 与 NODATA 分别处理")
    show(call("/api/query", {"name": "ghost.example.com", "qtype": "A"})["result"])
    show(call("/api/query", {"name": "ghost.example.com", "qtype": "A"})["result"])
    show(call("/api/query", {"name": "mail.example.org", "qtype": "A"})["result"])

    heading("3. 临时故障：ns1 离线，故障转移到 ns2 并进入健康冷却")
    call("/api/authority/mode", {"server": NS[0], "mode": "offline"})
    call("/api/cache/clear", {})
    show(call("/api/query", {"name": "mx.example.org", "qtype": "A"})["result"])

    heading("4. 全部权威离线 + TTL 过期：serve-stale 兜底")
    # ns1 仍离线：先经 ns2 重新建立 www 的正向缓存
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])
    call("/api/clock/advance", {"seconds": 301})  # 越过 www 的 300s TTL
    for name in NS[1:]:
        call("/api/authority/mode", {"server": name, "mode": "offline"})
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])

    heading("5. 越过 stale 宽限：旧缓存强制退出，转 SERVFAIL 故障短缓存")
    call("/api/clock/advance", {"seconds": 301})
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])

    heading("6. 权威恢复 + 故障 TTL 到期：重新回源，缓存刷新")
    call("/api/clock/advance", {"seconds": 6})
    for name in NS:
        call("/api/authority/mode", {"server": name, "mode": "normal"})
    show(call("/api/query", {"name": "www.example.com", "qtype": "A"})["result"])

    stats = call("/api/stats")
    heading("回源与命中统计")
    print("  每台权威回源次数:", stats["upstream"]["per_server"])
    print("  缓存命中:", stats["cache_hits"], " 未命中:", stats["cache_misses"])
    print("\n演示完成。打开查询台可继续手动实验：", BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
