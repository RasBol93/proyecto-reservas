import time
from collections import deque
from typing import Dict

from fastapi import HTTPException


class TenantRateLimiter:
    def __init__(self):
        self.buckets: Dict[str, deque] = {}
        self.window_sec = 60

    def hit(self, key: str, limit: int):
        now = time.time()
        dq = self.buckets.setdefault(key, deque())
        while dq and (now - dq[0]) > self.window_sec:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this tenant")
        dq.append(now)


rate_limiter = TenantRateLimiter()
