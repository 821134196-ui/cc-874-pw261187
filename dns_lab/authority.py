"""权威服务。

每个 :class:`AuthoritativeServer` 持有若干 :class:`Zone`（区域），
区域内可以配置 A/AAAA/SOA 等记录。权威服务有四种故障模式，
查询台可以随时切换：

* ``normal``      正常应答
* ``nxdomain``    对一切查询强制返回 NXDOMAIN（模拟数据被整体删除）
* ``servfail``    对一切查询强制返回 SERVFAIL（模拟权威进程异常）
* ``offline``     传输层直接报错（模拟宕机 / 网络不可达）

同一份区域数据会在多台权威之间复制（主/从副本），因此把一台打挂后
其余权威仍可应答；把所有权威都打挂才能触发解析器的 serve-stale。
"""

from __future__ import annotations

from .message import (
    AAAA,
    NXDOMAIN,
    NOERROR,
    SOA,
    SERVFAIL,
    Query,
    Record,
    Response,
    normalize_name,
)

MODES = ("normal", "nxdomain", "servfail", "offline")

_DEFAULT_SOA_SERIAL = 2026112600


class Zone:
    def __init__(self, name: str, records: list[Record] | None = None,
                 soa_minimum: int = 60) -> None:
        self.name = normalize_name(name)
        # rtype -> {owner: [Record]}，同一 owner/类型可有多条记录
        self._records: dict[int, dict[str, list[Record]]] = {}
        # NXDOMAIN / NODATA 否定应答携带的 SOA，minimum-ttl 决定否定缓存时长
        self.soa = Record(
            self.name, SOA,
            f"ns1.{self.name} hostmaster.{self.name} "
            f"{_DEFAULT_SOA_SERIAL} 7200 3600 1209600 {soa_minimum}",
            ttl=3600,
        )
        for rec in records or []:
            self.add_record(rec)

    def add_record(self, record: Record) -> None:
        record = Record(record.name, record.rtype, record.data, record.ttl)
        self._records.setdefault(record.rtype, {}).setdefault(
            record.name, []
        ).append(record)

    def remove_records(self, name: str, rtype: int | None = None) -> int:
        name = normalize_name(name)
        removed = 0
        types = [rtype] if rtype is not None else list(self._records)
        for t in types:
            bucket = self._records.get(t, {})
            if name in bucket:
                removed += len(bucket.pop(name))
        return removed

    def owns(self, qname: str) -> bool:
        qname = normalize_name(qname)
        return qname == self.name or qname.endswith("." + self.name)

    def answer(self, query: Query) -> Response:
        """区域内的正常应答逻辑（不考虑服务器故障模式）。"""
        answers = list(self._records.get(query.qtype, {}).get(query.name, []))
        if answers:
            return Response(NOERROR, answers=answers)
        # 名字存在但没有该类型 -> NODATA；名字完全不存在 -> NXDOMAIN
        name_exists = any(query.name in owners for owners in self._records.values())
        if name_exists:
            return Response(NOERROR, answers=[], authority=[self.soa])
        return Response(NXDOMAIN, answers=[], authority=[self.soa])

    def all_records(self) -> list[Record]:
        out: list[Record] = []
        for owners in self._records.values():
            for recs in owners.values():
                out.extend(recs)
        return sorted(out, key=lambda r: (r.name, r.rtype, r.data))

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "soa": _record_dict(self.soa),
            "records": [_record_dict(r) for r in self.all_records()],
        }


def _record_dict(r: Record) -> dict:
    return {"name": r.name, "rtype": r.rtype_name, "data": r.data, "ttl": r.ttl}


class AuthoritativeServer:
    def __init__(self, name: str, zones: list[Zone] | None = None) -> None:
        self.name = name
        self.zones: dict[str, Zone] = {}
        for z in zones or []:
            self.zones[z.name] = z
        self.mode = "normal"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}, expected one of {MODES}")
        self.mode = mode

    def query(self, query: Query) -> Response:
        """仅在 normal 模式下给出真实应答；其余模式模拟故障。

        ``offline`` 不会走到这里——传输层在调用前直接抛 UpstreamError。
        """
        if self.mode == "offline":
            # 双保险：即便绕过传输层也不给出应答
            raise ConnectionError(f"authority {self.name} is offline")
        if self.mode == "servfail":
            return Response(SERVFAIL, source=self.name)
        if self.mode == "nxdomain":
            return Response(NXDOMAIN, source=self.name)

        for zone in self.zones.values():
            if zone.owns(query.name):
                resp = zone.answer(query)
                resp.source = self.name
                return resp
        # 没有任何区域能回答该后缀：模拟真实世界里“无法到达相应权威”
        return Response(SERVFAIL, source=self.name)

    def find_zone(self, qname: str) -> Zone | None:
        qname = normalize_name(qname)
        best: Zone | None = None
        for zone in self.zones.values():
            if zone.owns(qname) and (best is None or len(zone.name) > len(best.name)):
                best = zone
        return best  # 最长后缀匹配

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "zones": [z.snapshot() for z in self.zones.values()],
        }
