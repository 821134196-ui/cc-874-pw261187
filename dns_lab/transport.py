"""模拟回源传输层。

负责把解析器的查询送到权威服务，并：

* 对 ``offline`` 权威直接抛 :class:`UpstreamError`（模拟网络不可达）；
* 统计每台权威实际收到的查询次数（“回源次数”，查询台展示的核心指标）；
* 支持给每台权威配置模拟网络延迟（基于受控时钟 sleep，冻结时钟时不真实等待）。
"""

from __future__ import annotations

import threading
from collections import defaultdict

from .authority import AuthoritativeServer
from .clock import ControlledClock
from .message import Query, Response, UpstreamError


class SimulatedTransport:
    def __init__(self, clock: ControlledClock,
                 authorities: list[AuthoritativeServer] | None = None) -> None:
        self.clock = clock
        self.authorities: dict[str, AuthoritativeServer] = {}
        # 每台权威的模拟单次往返延迟（秒），由受控时钟驱动
        self.latency: dict[str, float] = {}
        # 每台权威被实际访问的次数；offline 导致的失败尝试也计数
        self.query_counts: dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()
        for auth in authorities or []:
            self.add_authority(auth)

    def add_authority(self, server: AuthoritativeServer,
                      latency: float = 0.0) -> None:
        with self._lock:
            self.authorities[server.name] = server
            self.latency[server.name] = latency

    def query(self, server_name: str, query: Query) -> Response:
        with self._lock:
            server = self.authorities.get(server_name)
            if server is None:
                raise UpstreamError(f"unknown authority {server_name!r}")
            # 一次真实回源尝试：无论成功失败都计入回源次数
            self.query_counts[server_name] += 1
            delay = self.latency.get(server_name, 0.0)

        if delay > 0:
            # 冻结时钟时 advance 立即生效，不消耗真实时间
            self.clock.advance(delay)

        if server.mode == "offline":
            raise UpstreamError(
                f"authority {server_name} is offline (connection refused)"
            )
        try:
            return server.query(query)
        except ConnectionError as exc:
            raise UpstreamError(str(exc)) from exc

    def total_queries(self) -> int:
        return sum(self.query_counts.values())

    def reset_counters(self) -> None:
        with self._lock:
            self.query_counts.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self.total_queries(),
                "per_server": dict(self.query_counts),
                "latency": dict(self.latency),
            }
