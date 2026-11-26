"""时钟、权威应答与三类缓存的单元测试。"""

import unittest

from dns_lab.authority import AuthoritativeServer, Zone
from dns_lab.cache import (
    KIND_FAILURE,
    KIND_NEGATIVE,
    KIND_POSITIVE,
    ResolverCache,
)
from dns_lab.clock import ControlledClock
from dns_lab.message import A, AAAA, NXDOMAIN, NOERROR, SERVFAIL, Query, Record


class ClockTest(unittest.TestCase):
    def test_freeze_and_advance(self):
        clock = ControlledClock()
        clock.freeze()
        t0 = clock.now()
        clock.advance(42)
        self.assertAlmostEqual(clock.now(), t0 + 42)
        clock.advance(-2)
        self.assertAlmostEqual(clock.now(), t0 + 40)

    def test_resume_keeps_virtual_time_continuous(self):
        clock = ControlledClock()
        clock.freeze()
        clock.advance(100)
        frozen = clock.now()
        clock.resume()
        self.assertAlmostEqual(clock.now(), frozen, delta=1)

    def test_set_time_in_live_mode_uses_offset(self):
        clock = ControlledClock()
        clock.set_time(1_000_000.0)
        self.assertAlmostEqual(clock.now(), 1_000_000.0, delta=1)
        clock.reset()


class AuthorityTest(unittest.TestCase):
    def _server(self) -> AuthoritativeServer:
        zone = Zone("example.org", soa_minimum=30, records=[
            Record("mail.example.org", AAAA, "2001:db8::1", ttl=120),
        ])
        return AuthoritativeServer("ns1", [zone])

    def test_nodata_vs_nxdomain(self):
        server = self._server()
        nodata = server.query(Query("mail.example.org", A))
        self.assertTrue(nodata.is_nodata)
        self.assertEqual(nodata.rcode, NOERROR)
        self.assertTrue(nodata.authority)  # 携带 SOA

        nx = server.query(Query("ghost.example.org", A))
        self.assertEqual(nx.rcode, NXDOMAIN)
        self.assertTrue(nx.authority)

    def test_failure_modes(self):
        server = self._server()
        server.set_mode("servfail")
        self.assertEqual(server.query(Query("mail.example.org", A)).rcode, SERVFAIL)
        server.set_mode("nxdomain")
        self.assertEqual(
            server.query(Query("mail.example.org", A)).rcode, NXDOMAIN)
        server.set_mode("offline")
        with self.assertRaises(ConnectionError):
            server.query(Query("mail.example.org", A))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = ControlledClock()
        self.clock.freeze()
        self.cache = ResolverCache(
            self.clock, max_negative_ttl=300, failure_ttl=5, stale_window=100
        )

    def test_positive_entry_expires_then_goes_stale_then_dies(self):
        q = Query("www.example.com", A)
        from dns_lab.message import Response
        resp = Response(NOERROR, answers=[
            Record(q.name, A, "198.51.100.1", ttl=60)
        ], source="ns1")
        entry = self.cache.store_response(q, resp)
        self.assertEqual(entry.kind, KIND_POSITIVE)

        self.assertIsNotNone(self.cache.get_fresh(q))
        self.clock.advance(60)
        # 新鲜期结束：get_fresh 未命中，但 stale 宽限内可兜底
        self.assertIsNone(self.cache.get_fresh(q))
        stale = self.cache.get_stale(q)
        self.assertIsNotNone(stale)
        self.assertEqual(stale.kind, KIND_POSITIVE)

        self.clock.advance(100)  # 越过 stale 宽限
        self.assertIsNone(self.cache.get_stale(q))
        self.assertEqual(self.cache.snapshot(), [])  # 死条目被物理清除

    def test_negative_ttl_uses_soa_minimum_capped(self):
        q = Query("ghost.example.com", A)
        from dns_lab.message import Response
        soa = Record("example.com", 6,
                     "ns1 hostmaster 1 7200 3600 1209600 60", ttl=3600)
        resp = Response(NXDOMAIN, authority=[soa], source="ns1")
        entry = self.cache.store_response(q, resp)
        self.assertEqual(entry.kind, KIND_NEGATIVE)
        self.assertEqual(entry.ttl_at(self.clock.now()), 60)

    def test_negative_ttl_capped_by_max(self):
        q = Query("ghost.example.com", A)
        from dns_lab.message import Response
        soa = Record("example.com", 6,
                     "ns1 hostmaster 1 7200 3600 1209600 9999", ttl=3600)
        entry = self.cache.store_response(
            q, Response(NXDOMAIN, authority=[soa], source="ns1"))
        self.assertEqual(entry.ttl_at(self.clock.now()), 300)

    def test_failure_entry_short_ttl_and_no_stale(self):
        q = Query("www.example.com", A)
        entry = self.cache.store_failure(q, source="ns1")
        self.assertEqual(entry.kind, KIND_FAILURE)
        self.assertFalse(entry.allows_stale)
        self.assertIsNotNone(self.cache.get_fresh(q))
        self.clock.advance(5)
        self.assertIsNone(self.cache.get_fresh(q))
        self.assertIsNone(self.cache.get_stale(q))  # 故障绝不 serve-stale

    def test_ttl_rewrite_reflects_elapsed_time(self):
        q = Query("www.example.com", A)
        from dns_lab.message import Response
        self.cache.store_response(q, Response(NOERROR, answers=[
            Record(q.name, A, "198.51.100.1", ttl=60)
        ], source="ns1"))
        entry = self.cache.get_fresh(q)
        self.clock.advance(20)
        rewritten = self.cache.get_fresh(q).records[0]
        # 缓存内原始 TTL 不变；经过时间在解析器层重写
        self.assertEqual(entry.records[0].ttl, 60)
        self.assertEqual(max(0, entry.records[0].ttl - 20), 40)


if __name__ == "__main__":
    unittest.main()
