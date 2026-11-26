"""UDP 报文编解码与端到端 socket 测试。"""

import socket
import struct
import threading
import unittest

from dns_lab.clock import ControlledClock
from dns_lab.lab import build_default_lab
from dns_lab.message import A, AAAA, NXDOMAIN, Query
from dns_lab.udp_frontend import (
    UdpDnsFrontend,
    _decode_name,
    build_error_packet,
    parse_query_packet,
)


def _build_wire_query(msg_id: int, name: str, qtype: int) -> bytes:
    header = struct.pack("!6H", msg_id, 0x0100, 1, 0, 0, 0)
    question = b"".join(
        bytes([len(label)]) + label.encode() for label in name.split(".")
    ) + b"\x00" + struct.pack("!HH", qtype, 1)
    return header + question


class WireParsingTest(unittest.TestCase):
    def test_parse_simple_query(self):
        pkt = _build_wire_query(0x1234, "www.example.com", A)
        msg_id, q = parse_query_packet(pkt)
        self.assertEqual(msg_id, 0x1234)
        self.assertEqual(q.name, "www.example.com.")
        self.assertEqual(q.qtype, A)

    def test_parse_compressed_pointer(self):
        # 手工构造：先写一个名字，再用压缩指针引用它作为问题名
        header = struct.pack("!6H", 1, 0, 1, 0, 0, 0)
        name = b"\x03www\x07example\x03com\x00"
        pointer_question = struct.pack("!HHH", 0xC00C, A, 1)
        pkt = header + name + pointer_question
        msg_id, q = parse_query_packet(pkt)
        self.assertEqual(msg_id, 1)
        self.assertEqual(q.name, "www.example.com.")

    def test_parse_rejects_bad_packet(self):
        from dns_lab.udp_frontend import DnsWireError
        with self.assertRaises(DnsWireError):
            parse_query_packet(b"\x00" * 5)


class UdpEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.lab = build_default_lab()
        self.frontend = UdpDnsFrontend(
            self.lab["resolver"], "127.0.0.1", 0  # 由系统分配端口
        )
        self.frontend.start()
        self.addr = self.frontend._sock.getsockname()

    def tearDown(self):
        self.frontend.stop()

    def _ask(self, name: str, qtype: int = A) -> tuple[int, int, list[bytes]]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            sock.sendto(_build_wire_query(0xABCD, name, qtype), self.addr)
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        _msg_id, flags, _qd, ancount, _ns, _ar = struct.unpack("!6H", data[:12])
        rcode = flags & 0xF
        # 跳过 question 段（复用前端的压缩感知名称解码）
        offset = 12
        _qname, offset = _decode_name(data, offset)
        offset += 4  # qtype + qclass
        # 抽取应答 rdata：每条 RR 为 名称(指针2B) + 类型2B + 类2B + TTL4B + 长度2B
        rdata = []
        for _ in range(ancount):
            _name, rr_start = _decode_name(data, offset)
            _rtype, _rclass, _ttl, rdlen = struct.unpack(
                "!HHIH", data[rr_start:rr_start + 10])
            rdata.append(data[rr_start + 10:rr_start + 10 + rdlen])
            offset = rr_start + 10 + rdlen
        return rcode, ancount, rdata

    def test_a_query_returns_ip_and_then_cache_hit(self):
        rcode, ancount, rdata = self._ask("www.example.com", A)
        self.assertEqual(rcode, 0)
        self.assertEqual(ancount, 1)
        self.assertEqual(socket.inet_ntop(socket.AF_INET, rdata[0]),
                         "198.51.100.10")
        # 第二次：缓存命中，回源计数不再增加
        before = self.lab["transport"].total_queries()
        self._ask("www.example.com", A)
        self.assertEqual(self.lab["transport"].total_queries(), before)

    def test_aaaa_query(self):
        rcode, ancount, rdata = self._ask("www.example.com", AAAA)
        self.assertEqual(rcode, 0)
        self.assertEqual(
            socket.inet_ntop(socket.AF_INET6, rdata[0]), "2001:db8::10")

    def test_nxdomain_wire_rcode(self):
        rcode, _, _ = self._ask("ghost.example.com", A)
        self.assertEqual(rcode, NXDOMAIN)

    def test_malformed_packet_gets_formerr(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            sock.sendto(b"\x00\x01\x02", self.addr)
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        flags = struct.unpack("!H", data[2:4])[0]
        self.assertEqual(flags & 0xF, 1)  # FORMERR


if __name__ == "__main__":
    unittest.main()
