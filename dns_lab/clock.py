"""受控时钟。

支持三种状态：

* ``live``    —— 跟随真实墙钟（可带偏移）
* ``frozen``  —— 时间冻结，仅当手动 ``advance`` / ``set`` 时才前进
* 冻结后 ``resume`` 会保留当前虚拟时刻，以偏移方式回到 live 模式

整个实验台（缓存到期、故障冷却、serve-stale 宽限）全部读取这一个时钟，
因此在查询台上拨时钟即可确定性地观察缓存生命周期，无需真实等待。
"""

from __future__ import annotations

import threading
import time


class ControlledClock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._frozen = False
        self._frozen_at: float | None = None
        self._offset = 0.0  # virtual_time = real_time + offset（live 模式）

    def now(self) -> float:
        """当前虚拟时间（epoch 秒）。"""
        with self._lock:
            if self._frozen:
                assert self._frozen_at is not None
                return self._frozen_at
            return time.time() + self._offset

    def freeze(self) -> None:
        """冻结在当前虚拟时刻。"""
        with self._lock:
            if not self._frozen:
                self._frozen_at = self.now()
                self._frozen = True

    def resume(self) -> None:
        """恢复 live 模式，恢复瞬间的虚拟时间与冻结值连续。"""
        with self._lock:
            if self._frozen:
                assert self._frozen_at is not None
                self._offset = self._frozen_at - time.time()
                self._frozen = False
                self._frozen_at = None

    def advance(self, seconds: float) -> None:
        """向前（或向后，负数）拨指定秒数；live 模式下修改偏移。"""
        with self._lock:
            if self._frozen:
                assert self._frozen_at is not None
                self._frozen_at += seconds
            else:
                self._offset += seconds

    def set_time(self, epoch_seconds: float) -> None:
        """直接把虚拟时钟设到指定 epoch。"""
        with self._lock:
            if self._frozen:
                self._frozen_at = float(epoch_seconds)
            else:
                self._offset = float(epoch_seconds) - time.time()

    def reset(self) -> None:
        """回到真实墙钟、清除偏移与冻结。"""
        with self._lock:
            self._frozen = False
            self._frozen_at = None
            self._offset = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": "frozen" if self._frozen else "live",
                "epoch": self.now(),
                "offset": self._offset,
            }
