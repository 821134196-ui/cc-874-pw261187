"""真实 UDP DNS 协议前端。

让实验台可以被 ``dig @127.0.0.1 -p 5354 www.example.com`` 这类标准
客户端直接查询。仅实现实验所需子集：

* 查询：单问题、IN 类，支持带压缩指针的报文解析；
* 应答：A / AAAA 记录编码；NXDOMAIN / NODATA / SERVFAIL 通过 rcode 表达；
* 不做响应压缩（名称回指 0xc00c 已足够短），TC/AA 置 0、RA 置 1。

解析器内部仍是受控时钟驱动的模拟管线，UDP 线程只负责报文翻译。
"""

from __future__ import annotations

import socket
import struct
import threading

from .message import A, AAAA, NAME_TO_TYPE, Query, RCODE_NAMES
from .resolver import RecursiveResolver

IN_CLASS = 1
_PTR_QUESTION = 0xC00C  # 应答段名称回指报文开头的问题名


class DnsWireError(Exception):
    pass


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        encoded = label.encode("ascii")
        if len(encoded) > 63:
            raise DnsWireError("label too long")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _decode_name(buf: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen_pointers = set()
    while True:
        if offset >= len(buf):
            raise DnsWireError("name exceeds packet")
        length = buf[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if length & 0xC0 == 0xC0:  # 压缩指针
            if offset + 1 >= len(buf):
                raise DnsWireError("truncated pointer")
            pointer = ((length & 0x3F) << 8) | buf[offset + 1]
            if pointer in seen_pointers:
                raise DnsWireError("compression loop")
            seen_pointers.add(pointer)
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        if length & 0xC0:
            raise DnsWireError("unsupported label type")
        offset += 1
        labels.append(buf[offset:offset + length].decode("ascii"))
        offset += length
    return (".".join(labels) + ".") if labels else ".", next_offset


def parse_query_packet(data: bytes) -> tuple[int, Query]:
    if len(data) < 12:
        raise DnsWireError("packet shorter than header")
    msg_id, flags, qdcount, _anc, _nsc, _arc = struct.unpack("!6H", data[:12])
    if qdcount != 1:
        raise DnsWireError("exactly one question is supported")
    offset = 12
    qname, offset = _decode_name(data, offset)
    if offset + 4 > len(data):
        raise DnsWireError("truncated question")
    qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])
    if qclass != IN_CLASS:
        raise DnsWireError("only IN class supported")
    return msg_id, Query(qname, qtype)


def _encode_rdata(rtype: int, rdata: str) -> bytes:
    if rtype == A:
        return socket.inet_pton(socket.AF_INET, rdata)
    if rtype == AAAA:
        return socket.inet_pton(socket.AF_INET6, rdata)
    raise DnsWireError(f"rtype {rtype} cannot be encoded on wire frontend")


def build_response_packet(msg_id: int, query: Query, result) -> bytes:
    # QR=1 RD=1 RA=1，低 4 位携带解析器给出的 rcode（NXDOMAIN/NODATA/SERVFAIL）
    flags = 0x8180 | (result.rcode & 0xF)
    out = bytearray(struct.pack(
        "!6H", msg_id, flags, 1, len(result.answers), 0, 0
    ))
    out.extend(_encode_name(query.name))
    out.extend(struct.pack("!HH", query.qtype, IN_CLASS))
    for rec in result.answers:
        rdata = _encode_rdata(rec.rtype, rec.data)
        out.extend(struct.pack("!HHHIH",
                               _PTR_QUESTION, rec.rtype, IN_CLASS,
                               rec.ttl, len(rdata)))
        out.extend(rdata)
    return bytes(out)


def build_error_packet(msg_id: int, query: Query | None, rcode: int) -> bytes:
    flags = 0x8180 | (rcode & 0xF)
    out = bytearray(struct.pack("!6H", msg_id, flags, 1 if query else 0, 0, 0, 0))
    if query is not None:
        out.extend(_encode_name(query.name))
        out.extend(struct.pack("!HH", query.qtype, IN_CLASS))
    return bytes(out)


class UdpDnsFrontend:
    def __init__(self, resolver: RecursiveResolver, host: str = "127.0.0.1",
                 port: int = 5354) -> None:
        self.resolver = resolver
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(
            target=self._serve_loop, name="dns-udp-frontend", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg_id, query = parse_query_packet(data)
            except DnsWireError:
                # 连问题都解析不了：回 FORMERR 空问题
                msg_id = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 0
                self._safe_send(build_error_packet(msg_id, None, 1), addr)
                continue
            try:
                result = self.resolver.resolve(query)
                packet = build_response_packet(msg_id, query, result)
            except DnsWireError:
                packet = build_error_packet(msg_id, query, 4)  # NOTIMP
            self._safe_send(packet, addr)

    def _safe_send(self, packet: bytes, addr) -> None:
        try:
            assert self._sock is not None
            self._sock.sendto(packet, addr)
        except OSError:
            pass
