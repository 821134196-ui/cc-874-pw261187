"""本地 DNS 缓存解析器实验台。

模块划分：

* :mod:`dns_lab.clock`      —— 可冻结、可拨快、可回拨的受控时钟
* :mod:`dns_lab.message`    —— DNS 报文 / 记录 / 状态的简化模型
* :mod:`dns_lab.authority`  —— 权威服务（区域数据 + 故障模式）
* :mod:`dns_lab.transport`  —— 模拟回源传输层
* :mod:`dns_lab.cache`      —— 正向 / 否定 / 故障三类缓存
* :mod:`dns_lab.resolver`   —— 递归缓存解析器（故障转移 + serve-stale）
* :mod:`dns_lab.udp_frontend` —— 真实 UDP DNS 协议前端（可被 dig 查询）
* :mod:`dns_lab.web`        —— 本地查询台 HTTP 服务
"""

from .clock import ControlledClock
from .message import Query, Record, ResolveResult, Response, UpstreamError
from .authority import AuthoritativeServer, Zone
from .transport import SimulatedTransport
from .cache import ResolverCache
from .resolver import RecursiveResolver

__all__ = [
    "ControlledClock",
    "Query",
    "Record",
    "ResolveResult",
    "Response",
    "UpstreamError",
    "AuthoritativeServer",
    "Zone",
    "SimulatedTransport",
    "ResolverCache",
    "RecursiveResolver",
]
