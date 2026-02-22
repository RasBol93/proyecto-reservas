# app/rate_limit.py

import time
from collections import deque
from typing import Deque, Dict

from fastapi import HTTPException


class TenantRateLimiter:
    def __init__(self, window_sec: int = 60):
        self.window_sec = int(window_sec)
        self.buckets: Dict[str, Deque[float]] = {}

    def hit(self, key: str, limit: int) -> None:
        if limit <= 0:
            return  # sin limit

        now = time.time()
        dq = self.buckets.setdefault(key, deque())

        # limpiar eventos viejos
        cutoff = now - self.window_sec
        while dq and dq[0] <= cutoff:
            dq.popleft()

        if len(dq) >= limit:
            oldest = dq[0] if dq else now
            retry_after = max(0, int(self.window_sec - (now - oldest)))

            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "key": key,
                    "limit_per_min": limit,
                    "window_sec": self.window_sec,
                    "retry_after_sec": retry_after,
                },
            )

        dq.append(now)

        # limpieza: si por alguna razón quedó vacío (raro), borrar clave
        if not dq:
            self.buckets.pop(key, None)


rate_limiter = TenantRateLimiter(window_sec=60)
