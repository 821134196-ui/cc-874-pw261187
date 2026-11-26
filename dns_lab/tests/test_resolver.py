"""解析器端到端行为测试：缓存命中、故障转移、否定/故障缓存、serve-stale。"""

import unittest

from dns_lab.cache import ResolverCache
from dns_lab.clock import ControlledClock
from dns_lab.lab import build_default_lab
from dns_lab.message import A, NXDOMAIN, NOERROR, SERVFAIL, Query
from dns_lab.resolver import (
    ORIGIN_CACHE,
    ORIGIN_FAILURE_CACHE,
    ORIGIN_NEGATIVE_CACHE,
    ORIGIN_STALE,
    ORIGIN_UPSTREAM,
    ORIGIN_UPSTREAM_FAILURE,
)

NS1, NS2, NS3 = "ns1.example.net", "ns2.example.net", "ns3.example.net"


class ResolverBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.lab = build_default_lab(stale_window=300, failure_ttl=5)
        self.clock: ControlledClock = self.lab["clock"]
        self.clock.freeze()
        self.resolver = self.lab["resolver"]
        self.transport = self.lab["transport"]
        self.auth = {a.name: a for a in self.lab["authorities"]}

    def test_positive_cache_hit_avoids_upstream(self):
        q = Query("www.example.com", A)
        r1 = self.resolver.resolve(q)
        self.assertEqual(r1.origin, ORIGIN_UPSTREAM)
        self.assertEqual(r1.rcode, NOERROR)
        self.assertEqual(self.transport.query_counts[NS1], 1)

        r2 = self.resolver.resolve(q)
        self.assertEqual(r2.origin, ORIGIN_CACHE)
        self.assertFalse(r2.fetched)
        # 第二次完全不回源，计数不变
        self.assertEqual(self.transport.query_counts[NS1], 1)
        # TTL 随虚拟时间重写
        self.clock.advance(100)
        r3 = self.resolver.resolve(q)
        self.assertEqual(r3.answers[0].ttl, 200)  # 原始 300 - 100

    def test_nxdomain_and_nodata_use_negative_cache(self):
        q_nx = Query("nope.example.com", A)
        r = self.resolver.resolve(q_nx)
        self.assertEqual(r.rcode, NXDOMAIN)
        self.assertEqual(r.origin, ORIGIN_UPSTREAM)
        self.assertEqual(self.transport.total_queries(), 1)

        r2 = self.resolver.resolve(q_nx)
        self.assertEqual(r2.origin, ORIGIN_NEGATIVE_CACHE)
        self.assertEqual(self.transport.total_queries(), 1)

        # NODATA：mail.example.org 只有 AAAA
        q_nd = Query("mail.example.org", A)
        r3 = self.resolver.resolve(q_nd)
        self.assertTrue(r3.rcode == NOERROR and not r3.answers)
        r4 = self.resolver.resolve(q_nd)
        self.assertEqual(r4.origin, ORIGIN_NEGATIVE_CACHE)

    def test_failover_to_second_authority(self):
        self.auth[NS1].set_mode("offline")
        r = self.resolver.resolve(Query("www.example.com", A))
        self.assertEqual(r.origin, ORIGIN_UPSTREAM)
        self.assertEqual(r.source, f"权威 {NS2}")
        self.assertEqual(r.upstream_attempts, [NS1, NS2])
        self.assertEqual(self.transport.query_counts[NS1], 1)
        self.assertEqual(self.transport.query_counts[NS2], 1)

        # ns1 进入冷却：下一次查询直接跳过 ns1，只问 ns2
        r2 = self.resolver.resolve(Query("api.example.com", A))
        self.assertEqual(r2.upstream_attempts, [NS2])

        # 冷却结束后 ns1 重新参与选举（但仍离线，故重试后再转移到 ns2）；
        # 用一个未查询过的名字确保不命中缓存
        self.clock.advance(31)
        r3 = self.resolver.resolve(Query("fresh.example.org", A))
        self.assertEqual(r3.upstream_attempts, [NS1, NS2])

        # ns1 恢复后：新查询只尝试 ns1 即成功
        self.auth[NS1].set_mode("normal")
        self.clock.advance(31)  # 跳过 ns1 上一轮失败带来的冷却
        r4 = self.resolver.resolve(Query("recovered.example.org", A))
        self.assertEqual(r4.upstream_attempts, [NS1])

    def test_servfail_is_cached_as_failure_then_recovers(self):
        # 全部权威强制 SERVFAIL
        for a in self.auth.values():
            a.set_mode("servfail")
        q = Query("www.example.com", A)
        r1 = self.resolver.resolve(q)
        self.assertEqual(r1.rcode, SERVFAIL)
        self.assertEqual(r1.origin, ORIGIN_UPSTREAM_FAILURE)
        attempts_first = self.transport.total_queries()
        self.assertEqual(attempts_first, 3)  # 三台都试了一遍

        # failure_ttl(5s) 内命中故障缓存，不再回源
        r2 = self.resolver.resolve(q)
        self.assertEqual(r2.origin, ORIGIN_FAILURE_CACHE)
        self.assertEqual(self.transport.total_queries(), attempts_first)

        # 故障缓存到期且权威恢复 -> 重新回源拿到正向应答
        self.clock.advance(6)
        for a in self.auth.values():
            a.set_mode("normal")
        r3 = self.resolver.resolve(q)
        self.assertEqual(r3.rcode, NOERROR)
        self.assertEqual(r3.origin, ORIGIN_UPSTREAM)

    def test_serve_stale_when_all_authorities_down_and_expiry(self):
        q = Query("www.example.com", A)
        # 先拿到正向缓存（TTL 300）
        self.resolver.resolve(q)
        self.assertEqual(self.transport.total_queries(), 1)

        # TTL 过期
        self.clock.advance(301)
        for a in self.auth.values():
            a.set_mode("offline")

        r = self.resolver.resolve(q)
        self.assertEqual(r.origin, ORIGIN_STALE)
        self.assertEqual(r.rcode, NOERROR)
        self.assertTrue(r.answers)  # 陈旧记录仍可返回
        self.assertEqual(self.transport.total_queries(), 4)  # 触发一轮三台回源

        # stale 宽限（300s）内持续兜底，不再产生新缓存命中类型混乱
        self.clock.advance(100)
        r2 = self.resolver.resolve(q)
        self.assertEqual(r2.origin, ORIGIN_STALE)

        # 宽限期结束：陈旧条目强制退出，转为故障
        self.clock.advance(201)
        r3 = self.resolver.resolve(q)
        self.assertEqual(r3.origin, ORIGIN_UPSTREAM_FAILURE)
        self.assertEqual(r3.rcode, SERVFAIL)
        # 陈旧条目已被物理清除，缓存里只剩新写入的故障条目
        entries = self.lab["cache"].snapshot()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "failure")

    def test_recovered_authority_refreshes_stale_entry(self):
        q = Query("www.example.com", A)
        self.resolver.resolve(q)
        self.clock.advance(301)  # 缓存变 stale

        # 全部宕机 -> stale 兜底
        for a in self.auth.values():
            a.set_mode("offline")
        self.assertEqual(self.resolver.resolve(q).origin, ORIGIN_STALE)

        # 权威恢复：下一次查询应回源并重新写入新鲜缓存
        for a in self.auth.values():
            a.set_mode("normal")
        r = self.resolver.resolve(q)
        self.assertEqual(r.origin, ORIGIN_UPSTREAM)
        entry = self.lab["cache"].snapshot()[0]
        self.assertEqual(entry["state"], "fresh")
        self.assertEqual(entry["ttl"], 300)

    def test_transport_error_counts_as_upstream_attempt(self):
        self.auth[NS1].set_mode("offline")
        self.auth[NS2].set_mode("offline")
        r = self.resolver.resolve(Query("www.example.com", A))
        self.assertEqual(r.upstream_attempts, [NS1, NS2, NS3])
        self.assertEqual(r.rcode, NOERROR)  # ns3 正常应答
        self.assertEqual(self.transport.query_counts[NS1], 1)
        self.assertEqual(self.transport.query_counts[NS2], 1)
        self.assertEqual(self.transport.query_counts[NS3], 1)


if __name__ == "__main__":
    unittest.main()
