"""解析器缓存：正向、否定、故障三类条目分开存放与统计。

三类条目的 TTL 语义（参照 RFC 2308 / RFC 8767 / RFC 9520）：

========== ============================================================
正向条目   min(记录自身 TTL) 内权威应答可直接复用；过期后进入 stale 宽限
否定条目   NXDOMAIN / NODATA 取 SOA minimum-ttl（上限 max-negative-ttl）
故障条目   SERVFAIL / 传输异常只缓存极短时间（failure-ttl，默认 5s），
           且 **不允许** serve-stale
========== ============================================================

每条记录同时保存两个时刻：

* ``expires_at``  —— 正常 TTL 到期时刻，之后不能再作为新鲜缓存返回；
* ``stale_until`` —— serve-stale 宽限截止时刻（仅正向/否定条目），
                     超过该时刻陈旧条目必须退出，缓存槽被物理删除。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .clock import ControlledClock
from .message import (
    AAAA,
    NXDOMAIN,
    NOERROR,
    SERVFAIL,
    SOA,
    Query,
    Record,
    Response,
)

# 条目类别
KIND_POSITIVE = "positive"
KIND_NEGATIVE = "negative"
KIND_FAILURE = "failure"

KIND_LABELS = {
    KIND_POSITIVE: "正向缓存",
    KIND_NEGATIVE: "否定缓存",
    KIND_FAILURE: "故障缓存",
}


@dataclass
class CacheEntry:
    key: tuple[str, int]
    kind: str
    rcode: int
    records: list[Record]            # 应答段（正向）
    authority: list[Record]          # SOA 等权威段（否定）
    created_at: float
    expires_at: float                # 新鲜期截止
    stale_until: float               # 陈旧宽限截止；等于 expires_at 表示不允许 stale
    source: str                      # 首次写入该条目的权威名
    last_refresh: float = 0.0        # 最近一次成功回源刷新时刻

    @property
    def allows_stale(self) -> bool:
        return self.stale_until > self.expires_at

    def ttl_at(self, now: float) -> int:
        return max(0, int(self.expires_at - now))

    def stale_ttl_at(self, now: float) -> int:
        return max(0, int(self.stale_until - now))

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at

    def is_stale(self, now: float) -> bool:
        return self.expires_at <= now < self.stale_until

    def is_dead(self, now: float) -> bool:
        return now >= self.stale_until


class ResolverCache:
    def __init__(
        self,
        clock: ControlledClock,
        max_positive_ttl: int = 86400,
        max_negative_ttl: int = 300,
        failure_ttl: int = 5,
        stale_window: int = 86400,
    ) -> None:
        self.clock = clock
        self.max_positive_ttl = max_positive_ttl
        self.max_negative_ttl = max_negative_ttl
        self.failure_ttl = failure_ttl
        # RFC 8767 建议的 stale 宽限上限（1 天）；权威恢复后，
        # 正在使用的旧缓存最迟也会在 created + ttl + stale_window 时退出
        self.stale_window = stale_window
        self._entries: dict[tuple[str, int], CacheEntry] = {}
        self._lock = threading.RLock()
        self.hits = {"fresh": 0, "stale": 0, "negative": 0, "failure": 0}
        self.misses = 0

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_fresh(self, query: Query) -> CacheEntry | None:
        """返回新鲜条目；过期/不存在返回 None（触发回源）。"""
        with self._lock:
            self._sweep(query.key)
            entry = self._entries.get(query.key)
            now = self.clock.now()
            if entry is None or not entry.is_fresh(now):
                self.misses += 1
                return None
            if entry.kind == KIND_POSITIVE:
                self.hits["fresh"] += 1
            elif entry.kind == KIND_NEGATIVE:
                self.hits["negative"] += 1
            else:
                self.hits["failure"] += 1
            return entry

    def get_stale(self, query: Query) -> CacheEntry | None:
        """所有权威不可用时调用：返回 stale 宽限期内的旧条目。

        故障条目（SERVFAIL）永远不允许 serve-stale。
        """
        with self._lock:
            self._sweep(query.key)
            entry = self._entries.get(query.key)
            now = self.clock.now()
            if entry is None or entry.kind == KIND_FAILURE:
                return None
            if entry.is_stale(now):
                self.hits["stale"] += 1
                return entry
            return None

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def store_response(self, query: Query, response: Response) -> CacheEntry:
        with self._lock:
            now = self.clock.now()
            if response.rcode == NOERROR and response.answers:
                entry = self._build_positive(query, response, now)
            elif response.rcode in (NXDOMAIN,) or response.is_nodata:
                entry = self._build_negative(query, response, now)
            elif response.rcode == SERVFAIL:
                entry = self._build_failure(query, response, now)
            else:
                # 其他 rcode 不缓存
                self._entries.pop(query.key, None)
                raise ValueError(f"non-cacheable rcode {response.rcode}")
            self._entries[query.key] = entry
            return entry

    def store_failure(self, query: Query, source: str) -> CacheEntry:
        """传输层异常（没拿到报文）也按临时故障缓存 failure_ttl 秒。"""
        with self._lock:
            now = self.clock.now()
            entry = CacheEntry(
                key=query.key,
                kind=KIND_FAILURE,
                rcode=SERVFAIL,
                records=[],
                authority=[],
                created_at=now,
                expires_at=now + self.failure_ttl,
                stale_until=now + self.failure_ttl,  # 无 stale 宽限
                source=source,
                last_refresh=now,
            )
            self._entries[query.key] = entry
            return entry

    def _build_positive(self, query: Query, response: Response,
                        now: float) -> CacheEntry:
        ttl = min((r.ttl for r in response.answers), default=self.max_positive_ttl)
        ttl = min(ttl, self.max_positive_ttl)
        return CacheEntry(
            key=query.key,
            kind=KIND_POSITIVE,
            rcode=NOERROR,
            records=list(response.answers),
            authority=list(response.authority),
            created_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl + self.stale_window,
            source=response.source,
            last_refresh=now,
        )

    def _build_negative(self, query: Query, response: Response,
                        now: float) -> CacheEntry:
        # RFC 2308：否定缓存 TTL = min(SOA minimum, max-negative-ttl)
        soa = next((r for r in response.authority if r.rtype == SOA), None)
        if soa is not None:
            fields = soa.data.split()
            minimum = int(fields[-1]) if fields else self.max_negative_ttl
        else:
            minimum = self.max_negative_ttl
        ttl = max(0, min(minimum, self.max_negative_ttl))
        return CacheEntry(
            key=query.key,
            kind=KIND_NEGATIVE,
            rcode=response.rcode,
            records=[],
            authority=list(response.authority),
            created_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl + self.stale_window,
            source=response.source,
            last_refresh=now,
        )

    def _build_failure(self, query: Query, response: Response,
                       now: float) -> CacheEntry:
        return CacheEntry(
            key=query.key,
            kind=KIND_FAILURE,
            rcode=SERVFAIL,
            records=[],
            authority=[],
            created_at=now,
            expires_at=now + self.failure_ttl,
            stale_until=now + self.failure_ttl,  # 故障不 stale
            source=response.source or "unknown",
            last_refresh=now,
        )

    # ------------------------------------------------------------------ #
    # 维护 / 展示
    # ------------------------------------------------------------------ #
    def _sweep(self, key: tuple[str, int] | None = None) -> int:
        """物理删除已过 stale 宽限（或 failure TTL）的死条目。"""
        now = self.clock.now()
        removed = 0
        if key is not None:
            entry = self._entries.get(key)
            if entry is not None and entry.is_dead(now):
                del self._entries[key]
                removed += 1
            return removed
        for dead_key in [k for k, e in self._entries.items() if e.is_dead(now)]:
            del self._entries[dead_key]
            removed += 1
        return removed

    def sweep(self) -> int:
        with self._lock:
            return self._sweep()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = {"fresh": 0, "stale": 0, "negative": 0, "failure": 0}
            self.misses = 0

    def snapshot(self) -> list[dict]:
        with self._lock:
            self._sweep()
            now = self.clock.now()
            out = []
            for entry in self._entries.values():
                state = (
                    "fresh" if entry.is_fresh(now)
                    else "stale" if entry.is_stale(now)
                    else "dead"
                )
                out.append({
                    "name": entry.key[0],
                    "qtype": entry.key[1],
                    "kind": entry.kind,
                    "kind_label": KIND_LABELS[entry.kind],
                    "rcode": entry.rcode,
                    "state": state,
                    "ttl": entry.ttl_at(now),
                    "stale_ttl": entry.stale_ttl_at(now),
                    "source": entry.source,
                    "records": [
                        {"name": r.name, "rtype": r.rtype_name,
                         "data": r.data, "ttl": r.ttl}
                        for r in entry.records
                    ],
                })
            return sorted(out, key=lambda e: (e["kind"], e["name"]))
