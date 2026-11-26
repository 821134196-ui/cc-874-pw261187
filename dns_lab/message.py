"""DNS 报文的简化内存模型。

不追求覆盖完整 RFC，只保留实验所需语义：查询名/类型、应答记录、
NXDOMAIN / NODATA / SERVFAIL、否定缓存所需的 SOA 记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 常用记录类型
A = 1
NS = 2
CNAME = 5
SOA = 6
AAAA = 28

TYPE_NAMES = {A: "A", NS: "NS", CNAME: "CNAME", SOA: "SOA", AAAA: "AAAA"}
NAME_TO_TYPE = {v: k for k, v in TYPE_NAMES.items()}

# 响应码
NOERROR = 0
FORMERR = 1
SERVFAIL = 2
NXDOMAIN = 3
NOTIMP = 4
REFUSED = 5

RCODE_NAMES = {
    NOERROR: "NOERROR",
    FORMERR: "FORMERR",
    SERVFAIL: "SERVFAIL",
    NXDOMAIN: "NXDOMAIN",
    NOTIMP: "NOTIMP",
    REFUSED: "REFUSED",
}


def normalize_name(name: str) -> str:
    """统一为小写、末尾带点的绝对域名。"""
    name = name.strip().lower()
    if not name:
        raise ValueError("empty domain name")
    if not name.endswith("."):
        name += "."
    return name


@dataclass
class Query:
    name: str
    qtype: int = A

    def __post_init__(self) -> None:
        self.name = normalize_name(self.name)
        if isinstance(self.qtype, str):
            self.qtype = NAME_TO_TYPE[self.qtype.upper()]

    @property
    def qtype_name(self) -> str:
        return TYPE_NAMES.get(self.qtype, str(self.qtype))

    @property
    def key(self) -> tuple[str, int]:
        return (self.name, self.qtype)


@dataclass
class Record:
    name: str
    rtype: int
    data: str
    ttl: int

    def __post_init__(self) -> None:
        self.name = normalize_name(self.name)
        self.ttl = max(0, int(self.ttl))

    @property
    def rtype_name(self) -> str:
        return TYPE_NAMES.get(self.rtype, str(self.rtype))

    def with_ttl(self, ttl: int) -> "Record":
        return Record(self.name, self.rtype, self.data, ttl)


@dataclass
class Response:
    """权威服务给出的应答。"""

    rcode: int
    answers: list[Record] = field(default_factory=list)
    authority: list[Record] = field(default_factory=list)  # NXDOMAIN/NODATA 时携带 SOA
    source: str = ""  # 产生该应答的权威服务名

    @property
    def is_nxdomain(self) -> bool:
        return self.rcode == NXDOMAIN

    @property
    def is_nodata(self) -> bool:
        """RFC 2308 定义的 NODATA：NOERROR 但应答段为空。"""
        return self.rcode == NOERROR and not self.answers

    @property
    def is_failure(self) -> bool:
        return self.rcode == SERVFAIL


class UpstreamError(Exception):
    """回源传输层故障（超时 / 连接拒绝 / 权威离线）。

    与权威明确返回的 SERVFAIL 区分：前者根本没拿到报文，后者拿到了
    rcode=SERVFAIL 的报文，两者在解析器里按同一类临时故障处理。
    """


@dataclass
class ResolveResult:
    """解析器返回给客户端的最终结果。"""

    query: Query
    rcode: int
    answers: list[Record]
    origin: str  # cache / upstream / negative-cache / failure-cache / stale-cache / upstream-failure
    source: str  # 具体出处：权威服务名或缓存条目说明
    fetched: bool  # 本次是否真正发起了回源
    upstream_attempts: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def rcode_name(self) -> str:
        return RCODE_NAMES.get(self.rcode, str(self.rcode))
