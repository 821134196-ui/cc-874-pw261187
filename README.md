# 本地 DNS 缓存解析器实验台

一个零依赖（纯 Python 标准库）的 DNS 递归缓存解析器教学/实验环境，包含
三台权威服务、一个带 Web 查询台的缓存解析器，以及一个可被 `dig` 直接
查询的真实 UDP DNS 前端。所有时间由**受控时钟**驱动，可以冻结、拨快、
回拨，从而确定性地观察缓存的完整生命周期。

## 启动

```bash
python -m dns_lab
# 查询台:    http://127.0.0.1:8080
# UDP DNS:  127.0.0.1:5354
```

可选参数：`--web-port` / `--dns-port` / `--no-udp` /
`--stale-window`（serve-stale 宽限，默认 300s）/
`--failure-ttl`（故障缓存，默认 5s）。

## 拓扑

```
                ┌──────────────────────────────┐
  浏览器查询台 ──▶│  RecursiveResolver（缓存解析器）│◀── dig / 任意 DNS 客户端
                │  三类缓存 + 故障冷却 + stale   │     (UDP 5354)
                └──────────────┬───────────────┘
              按 ns1→ns2→ns3 顺序回源，带故障转移
        ┌──────────────┬───────┴───────┬──────────────┐
   ns1.example.net ns2.example.net ns3.example.net   （各自持有独立区域副本）
        └──────────────┴───────────────┴──────────────┘
              example.com / example.org 两个区域
```

区域初始数据：

| 域名 | 记录 | TTL | 用途 |
| --- | --- | --- | --- |
| `www.example.com` | A `198.51.100.10` + AAAA `2001:db8::10` | 300 | 正向缓存 |
| `api.example.com` | A `198.51.100.20` | 60 | 短 TTL，快速观察到期 |
| `mail.example.org` | 仅 AAAA `2001:db8::25` | 120 | 查 A 即 **NODATA** |
| `mx.example.org` | A `198.51.100.30` | 180 | 正向 |
| 任意不存在的名字 | — | — | **NXDOMAIN**（SOA minimum=60s） |

## 三类响应分别如何处理

| 类别 | 触发 | 缓存 TTL | serve-stale | 出处标签 |
| --- | --- | --- | --- | --- |
| **正向缓存** | NOERROR 且有应答 | `min(记录 TTL, max-positive-ttl=86400)` | 允许，宽限 `stale-window` | `cache` / `upstream` |
| **否定缓存** | NXDOMAIN 或 NODATA（RFC 2308） | `min(SOA minimum-ttl, max-negative-ttl=300)` | 允许 | `negative-cache` |
| **故障缓存** | SERVFAIL 报文或传输异常（超时/离线） | 固定 `failure-ttl=5s`（RFC 9520 思路） | **不允许** | `failure-cache` / `upstream-failure` |

### 权威恢复后，旧缓存如何退出

1. 正向/否定条目 TTL 到期后进入 **serve-stale** 宽限（RFC 8767）：
   所有权威都不可用时，过期条目仍可兜底应答，页面标注
   `stale-cache` 并显示宽限剩余时间；
2. 宽限期内任一权威恢复，下一次查询即重新回源，旧条目被新鲜应答覆盖；
3. 宽限期结束权威仍未恢复，陈旧条目被**物理删除**，查询转为 SERVFAIL
   并写入 5 秒故障短缓存——即“正在使用的旧缓存也在期限内退出”。
4. SERVFAIL 故障条目没有 stale 宽限，到期即删，绝不把故障当陈旧数据返回。

## 查询台能做什么

- **受控时钟**：冻结/恢复、±5s/30s/60s/5min 快拨、自定义秒数、直接设定 epoch；
- **发起查询**：A/AAAA 等，结果展示 rcode、**返回记录的出处**（哪台权威
  或哪类缓存）、是否回源、依次尝试过的权威、TTL 剩余与说明；
- **修改权威回复**：每台权威可切 `正常 / 强制 NXDOMAIN / 强制 SERVFAIL /
  离线` 四种模式，可增删改区域记录与 TTL（三台副本相互独立，便于观察
  数据不一致时的故障转移）；
- **回源次数**：按权威分别统计实际收到的查询次数（离线尝试也计数），
  同时展示正向/否定/故障/stale 命中数与缓存未命中数；
- **缓存视图**：三类条目分显，含新鲜/陈旧状态、TTL、stale 剩余、首次出处；
- **查询日志**：最近 100 次查询的完整决策轨迹。

## 推荐实验路径

1. 查询 `www.example.com` A：第一次回源 ns1，再查零回源（正向缓存命中）；
2. 查询 `ghost.example.com`：得到 NXDOMAIN，再查命中否定缓存；
   查询 `mail.example.org` A：观察 NODATA 同样走否定缓存；
3. 冻结时钟，把 `www` 缓存拨到过期，把三台权威全部切 **离线**，
   再查询：得到 `stale-cache` 兜底，应答 TTL=0 但数据仍在；
4. 继续拨过 300s stale 宽限：旧缓存消失，返回 SERVFAIL；5s 内重查命中
   故障缓存（回源计数不增加）；
5. 把权威切回 **正常** 并拨过故障 TTL：重新回源，缓存刷新为新鲜；
6. 只把 ns1 切离线：查询自动转移 ns2，页面可见尝试链
   `ns1 → ns2` 与 ns1 的 30s 健康冷却。

也可以直接运行脚本化演示（需要先启动服务）：

```bash
python -m dns_lab &
python demo/demo_scenario.py
```

## 测试

```bash
python -m unittest discover -s dns_lab/tests -t . -v
```

24 个用例覆盖：时钟语义、权威 NXDOMAIN/NODATA 与故障模式、三类缓存的
TTL/到期/stale 退出、解析器故障转移与冷却、serve-stale 全周期、
故障短缓存与恢复、UDP 报文编解码（含压缩指针）与 socket 端到端。

## 代码结构

```
dns_lab/
├── clock.py          受控时钟（live/freeze/advance/set）
├── message.py        DNS 记录/查询/应答的简化模型
├── authority.py      权威服务：区域数据 + 四种故障模式
├── transport.py      模拟回源传输 + 回源计数
├── cache.py          三类缓存条目、TTL、stale 宽限、清扫
├── resolver.py       解析决策：缓存→故障转移→stale→故障短缓存
├── lab.py            默认拓扑装配
├── udp_frontend.py   真实 UDP DNS 协议（A/AAAA，dig 可查）
├── web.py            查询台 HTTP API
├── web_static/       单页控制台（原生 HTML/CSS/JS，无构建）
└── tests/            单元与端到端测试
```

## 与真实 RFC 的对应与取舍

- 否定缓存 TTL 取 SOA minimum 并受 max-negative-ttl 限制：RFC 2308；
- 故障期短缓存 SERVFAIL（默认 5s，建议 1–5s）：RFC 9520；
- 过期记录在全权威不可用时继续兜底、宽限上限 1 天：RFC 8767
  （实验默认缩到 300s 方便观察，可用 `--stale-window 86400` 还原）；
- 取舍：模拟权威而非真实 DNS 服务器（消息模型简化、无 DNSSEC/CNAME 链/
  分片），UDP 前端仅实现 IN 类 A/AAAA 与基本 rcode，足以支撑缓存语义实验。
