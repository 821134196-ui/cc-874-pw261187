"""递归缓存解析器。

单次 :meth:`resolve` 的决策流程：

1. 查缓存：
   - 命中新鲜正向/否定/故障条目 → 直接返回，不回源；
   - 仅存在过期条目 → 进入回源流程，必要时再作为 stale 兜底。
2. 回源：按顺序尝试权威，处于故障冷却期的权威暂时跳过（全部冷却时
   仍强制重试一轮）。任何一台给出确定应答（NOERROR/NXDOMAIN/NODATA/
   SERVFAIL 报文）即停止；传输异常或 SERVFAIL 触发冷却并尝试下一台。
3. 回源成功 → 写入对应类别缓存（正向/否定/故障），返回出处为权威。
4. 全部权威失败：
   - 存在过期的正向/否定条目 → serve-stale（RFC 8767），标注
     ``stale-cache``，并附剩余陈旧宽限；
   - 否则缓存一个短 TTL 故障条目并返回 SERVFAIL（RFC 9520 思路，
     防止故障风暴下反复回源）。
"""

from __future__ import annotations

import threading
from dataclasses import asdict

from .cache import (
    KIND_FAILURE,
    KIND_NEGATIVE,
    KIND_POSITIVE,
    CacheEntry,
    ResolverCache,
)
from .clock import ControlledClock
from .message import (
    NXDOMAIN,
    NOERROR,
    RCODE_NAMES,
    SERVFAIL,
    Query,
    Record,
    ResolveResult,
    Response,
    UpstreamError,
)
from .transport import SimulatedTransport

# 出处标签（查询台“返回记录的出处”直接展示）
ORIGIN_CACHE = "cache"
ORIGIN_NEGATIVE_CACHE = "negative-cache"
ORIGIN_FAILURE_CACHE = "failure-cache"
ORIGIN_STALE = "stale-cache"
ORIGIN_UPSTREAM = "upstream"
ORIGIN_UPSTREAM_FAILURE = "upstream-failure"

ORIGIN_LABELS = {
    ORIGIN_CACHE: "正向缓存命中",
    ORIGIN_NEGATIVE_CACHE: "否定缓存命中（NXDOMAIN/NODATA）",
    ORIGIN_FAILURE_CACHE: "故障缓存命中（SERVFAIL 短缓存）",
    ORIGIN_STALE: "陈旧缓存兜底（serve-stale）",
    ORIGIN_UPSTREAM: "权威回源",
    ORIGIN_UPSTREAM_FAILURE: "回源全部失败",
}


class RecursiveResolver:
    def __init__(
        self,
        clock: ControlledClock,
        transport: SimulatedTransport,
        upstream_order: list[str],
        cache: ResolverCache | None = None,
        health_cooldown: float = 30.0,
        log_size: int = 100,
    ) -> None:
        self.clock = clock
        self.transport = transport
        self.upstream_order = list(upstream_order)
        self.cache = cache or ResolverCache(clock)
        self.health_cooldown = health_cooldown
        self._cooldown_until: dict[str, float] = {}
        self._log: list[dict] = []
        self._log_size = log_size
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def resolve(self, query: Query) -> ResolveResult:
        # 1. 新鲜缓存
        fresh = self.cache.get_fresh(query)
        if fresh is not None:
            result = self._result_from_cache(query, fresh, fetched=False)
            self._append_log(result)
            return result

        # 2. 回源（带故障转移）
        response, attempts, errors = self._query_upstreams(query)
        if response is not None:
            entry = self.cache.store_response(query, response)
            result = self._result_from_upstream(query, entry, attempts)
            self._append_log(result)
            return result

        # 3. 全部权威失败：先尝试 serve-stale
        stale = self.cache.get_stale(query)
        if stale is not None:
            result = self._result_from_stale(query, stale, attempts, errors)
            self._append_log(result)
            return result

        # 4. 无陈旧缓存：缓存短 TTL 故障并返回 SERVFAIL
        entry = self.cache.store_failure(query, source=",".join(attempts) or "none")
        result = ResolveResult(
            query=query,
            rcode=SERVFAIL,
            answers=[],
            origin=ORIGIN_UPSTREAM_FAILURE,
            source=f"全部权威不可达（故障缓存 {self.cache.failure_ttl}s）",
            fetched=True,
            upstream_attempts=attempts,
            note="；".join(errors) if errors else "无可用权威",
        )
        self._append_log(result)
        return result

    # ------------------------------------------------------------------ #
    def _query_upstreams(
        self, query: Query
    ) -> tuple[Response | None, list[str], list[str]]:
        """按顺序回源，返回 (确定应答或 None, 尝试过的权威, 错误说明)。"""
        now = self.clock.now()
        healthy = [
            name for name in self.upstream_order
            if self._cooldown_until.get(name, 0.0) <= now
        ]
        # 全部处于冷却期时不要干等：强制重试一轮
        candidates = healthy if healthy else list(self.upstream_order)

        attempts: list[str] = []
        errors: list[str] = []
        last_negative: Response | None = None
        for name in candidates:
            attempts.append(name)
            try:
                response = self.transport.query(name, query)
            except UpstreamError as exc:
                self._mark_cooldown(name)
                errors.append(f"{name}: {exc}")
                continue

            if response.rcode == SERVFAIL:
                # 权威明确返回 SERVFAIL：视为临时故障，转移到下一台
                self._mark_cooldown(name)
                errors.append(f"{name}: SERVFAIL")
                continue

            if response.is_nxdomain and self._is_injected_failure(name):
                # 页面上把权威切成“强制 NXDOMAIN”模式属于注入故障：
                # 该权威的区域数据可能已被破坏，转移询问其余副本；
                # 所有副本都处于该模式时，最后一台的应答作为结论返回。
                self._mark_cooldown(name)
                errors.append(f"{name}: 强制 NXDOMAIN（故障模式）")
                last_negative = response
                continue

            # 收到确定应答（NOERROR 含 NODATA、正常的 NXDOMAIN）——恢复健康
            self._cooldown_until.pop(name, None)
            return response, attempts, errors

        # 所有权威都处于注入式 NXDOMAIN 模式：以最后一台的应答作为确定结论
        if last_negative is not None:
            self._cooldown_until.pop(attempts[-1], None)
            return last_negative, attempts, errors

        return None, attempts, errors

    def _is_injected_failure(self, server_name: str) -> bool:
        server = self.transport.authorities.get(server_name)
        return server is not None and server.mode == "nxdomain"

    def _mark_cooldown(self, name: str) -> None:
        with self._lock:
            self._cooldown_until[name] = self.clock.now() + self.health_cooldown

    def health_snapshot(self) -> dict:
        now = self.clock.now()
        return {
            name: {
                "in_cooldown": self._cooldown_until.get(name, 0.0) > now,
                "cooldown_remaining": max(0, int(self._cooldown_until.get(name, 0.0) - now)),
            }
            for name in self.upstream_order
        }

    # ------------------------------------------------------------------ #
    def _rewrite_ttls(self, entry: CacheEntry) -> list[Record]:
        """按经过时间重写记录 TTL，模拟真实 DNS 转发应答。"""
        elapsed = self.clock.now() - entry.created_at
        return [
            r.with_ttl(max(0, int(r.ttl - elapsed)))
            for r in entry.records
        ]

    def _result_from_cache(self, query: Query, entry: CacheEntry,
                           fetched: bool) -> ResolveResult:
        if entry.kind == KIND_POSITIVE:
            origin = ORIGIN_CACHE
            note = f"TTL 剩余 {entry.ttl_at(self.clock.now())}s"
        elif entry.kind == KIND_NEGATIVE:
            origin = ORIGIN_NEGATIVE_CACHE
            note = f"否定缓存 TTL 剩余 {entry.ttl_at(self.clock.now())}s"
        else:
            origin = ORIGIN_FAILURE_CACHE
            note = f"故障缓存 TTL 剩余 {entry.ttl_at(self.clock.now())}s"
        return ResolveResult(
            query=query,
            rcode=entry.rcode,
            answers=self._rewrite_ttls(entry),
            origin=origin,
            source=f"缓存（源自权威 {entry.source}）",
            fetched=False,
            note=note,
        )

    def _result_from_upstream(self, query: Query, entry: CacheEntry,
                              attempts: list[str]) -> ResolveResult:
        if entry.kind == KIND_NEGATIVE:
            note = "权威返回否定应答，已写入否定缓存"
        elif entry.kind == KIND_FAILURE:
            note = "权威返回 SERVFAIL，已写入短 TTL 故障缓存"
        else:
            note = "权威应答已写入正向缓存"
        return ResolveResult(
            query=query,
            rcode=entry.rcode,
            answers=list(entry.records),
            origin=ORIGIN_UPSTREAM,
            source=f"权威 {entry.source}",
            fetched=True,
            upstream_attempts=attempts,
            note=note,
        )

    def _result_from_stale(self, query: Query, entry: CacheEntry,
                           attempts: list[str], errors: list[str]) -> ResolveResult:
        kind = "否定" if entry.kind == KIND_NEGATIVE else "正向"
        note = (
            f"{kind}缓存已过期 {int(self.clock.now() - entry.expires_at)}s，"
            f"在 serve-stale 宽限内兜底；宽限剩余 {entry.stale_ttl_at(self.clock.now())}s，"
            f"到期后该条目强制退出。回源故障：{'；'.join(errors)}"
        )
        return ResolveResult(
            query=query,
            rcode=entry.rcode,
            answers=self._rewrite_ttls(entry),
            origin=ORIGIN_STALE,
            source=f"陈旧缓存（最后成功回源自 {entry.source}）",
            fetched=True,
            upstream_attempts=attempts,
            note=note,
        )

    # ------------------------------------------------------------------ #
    def _append_log(self, result: ResolveResult) -> None:
        with self._lock:
            d = asdict(result)
            d["time"] = self.clock.now()
            d["origin_label"] = ORIGIN_LABELS.get(result.origin, result.origin)
            d["rcode_name"] = result.rcode_name
            d["qtype_name"] = result.query.qtype_name
            self._log.append(d)
            if len(self._log) > self._log_size:
                self._log = self._log[-self._log_size:]

    def query_log(self) -> list[dict]:
        with self._lock:
            return list(reversed(self._log))

    def clear_log(self) -> None:
        with self._lock:
            self._log.clear()

    def stats_snapshot(self) -> dict:
        return {
            "cache_hits": dict(self.cache.hits),
            "cache_misses": self.cache.misses,
            "upstream": self.transport.snapshot(),
            "health": self.health_snapshot(),
        }
