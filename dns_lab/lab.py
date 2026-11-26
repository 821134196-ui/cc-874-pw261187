"""默认实验拓扑与装配。

三台权威（ns1 / ns2 / ns3）持有相同区域的副本，模拟主从部署；
解析器按 ns1 → ns2 → ns3 顺序回源，前一台故障自动转移。

区域数据：

* ``example.com`` —— www（A+AAAA，TTL 300）、api（A，TTL 60，便于观察到期）
* ``example.org`` —— mail 只有 AAAA（查 A 即 NODATA）；不存在的名字返回 NXDOMAIN
"""

from __future__ import annotations

from .authority import AuthoritativeServer, Zone
from .cache import ResolverCache
from .clock import ControlledClock
from .message import A, AAAA, Record
from .resolver import RecursiveResolver
from .transport import SimulatedTransport

# 可调的全局时序参数（秒）
DEFAULT_MAX_POSITIVE_TTL = 86400
DEFAULT_MAX_NEGATIVE_TTL = 300   # RFC 2308 否定缓存上限
DEFAULT_FAILURE_TTL = 5          # SERVFAIL/传输故障短缓存
DEFAULT_STALE_WINDOW = 300       # serve-stale 宽限（实验用 5 分钟，RFC 8767 上限 1 天）
DEFAULT_HEALTH_COOLDOWN = 30     # 权威故障后的冷却期


def _build_zones() -> list[Zone]:
    com = Zone("example.com", soa_minimum=60, records=[
        Record("www.example.com", A, "198.51.100.10", ttl=300),
        Record("www.example.com", AAAA, "2001:db8::10", ttl=300),
        Record("api.example.com", A, "198.51.100.20", ttl=60),
    ])
    org = Zone("example.org", soa_minimum=60, records=[
        # 故意只有 AAAA：对 mail.example.org 发 A 查询会得到 NODATA
        Record("mail.example.org", AAAA, "2001:db8::25", ttl=120),
        Record("mx.example.org", A, "198.51.100.30", ttl=180),
    ])
    return [com, org]


def build_default_lab(
    stale_window: int = DEFAULT_STALE_WINDOW,
    failure_ttl: int = DEFAULT_FAILURE_TTL,
) -> dict:
    clock = ControlledClock()
    zones_per_server = _build_zones()

    authorities: list[AuthoritativeServer] = []
    for name in ("ns1.example.net", "ns2.example.net", "ns3.example.net"):
        # 每台权威独立持有一份区域副本（独立对象，便于分别改数据/打故障）
        zones = _build_zones()
        authorities.append(AuthoritativeServer(name, zones))

    transport = SimulatedTransport(clock, authorities)
    for name in ("ns1.example.net", "ns2.example.net", "ns3.example.net"):
        transport.latency[name] = 0.0

    cache = ResolverCache(
        clock,
        max_positive_ttl=DEFAULT_MAX_POSITIVE_TTL,
        max_negative_ttl=DEFAULT_MAX_NEGATIVE_TTL,
        failure_ttl=failure_ttl,
        stale_window=stale_window,
    )
    resolver = RecursiveResolver(
        clock,
        transport,
        upstream_order=[a.name for a in authorities],
        cache=cache,
        health_cooldown=DEFAULT_HEALTH_COOLDOWN,
    )
    return {
        "clock": clock,
        "authorities": authorities,
        "transport": transport,
        "cache": cache,
        "resolver": resolver,
    }
