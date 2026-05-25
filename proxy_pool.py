"""SOCKS5 proxy pool — load, round-robin, health-check."""

import asyncio
import os
import random
from typing import Optional

import httpx

PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "proxies.txt")


def parse_proxy_line(line: str) -> Optional[str]:
    """Parse 'host:port:pass:user' into socks5:// URL. Skip comments/empty."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split(":")
    if len(parts) == 4:
        host, port, pwd, user = parts
        # URL-encode the password in case it contains special chars
        from urllib.parse import quote
        return f"socks5://{quote(user)}:{quote(pwd)}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        return f"socks5://{host}:{port}"
    return None


def load_proxies() -> list[str]:
    """Load all proxy URLs from the proxies file."""
    if not os.path.exists(PROXIES_FILE):
        return []
    proxies = []
    with open(PROXIES_FILE) as f:
        for line in f:
            url = parse_proxy_line(line)
            if url:
                proxies.append(url)
    return proxies


class ProxyPool:
    """Thread-safe proxy pool with round-robin / random selection.

    Maintains an availability set — dead proxies get removed temporarily
    and can be rechecked later.
    """

    def __init__(self, proxies: Optional[list[str]] = None):
        self._all = proxies or load_proxies()
        self._alive: set[str] = set(self._all)
        self._idx = 0
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._all)

    @property
    def alive_count(self) -> int:
        return len(self._alive)

    def get_random(self) -> Optional[str]:
        """Pick a random alive proxy."""
        if not self._alive:
            return None
        return random.choice(list(self._alive))

    async def get_round_robin(self) -> Optional[str]:
        """Pick the next alive proxy (round-robin)."""
        async with self._lock:
            if not self._alive:
                return None
            alive = list(self._alive)
            if self._idx >= len(alive):
                self._idx = 0
            proxy = alive[self._idx]
            self._idx = (self._idx + 1) % len(alive)
            return proxy

    async def mark_dead(self, proxy_url: str):
        """Remove a proxy from the alive set (will be rechecked later)."""
        async with self._lock:
            self._alive.discard(proxy_url)

    async def mark_alive(self, proxy_url: str):
        """Re-add a proxy to the alive set."""
        async with self._lock:
            if proxy_url in self._all:
                self._alive.add(proxy_url)

    def stats(self) -> dict:
        return {
            "total": self.count,
            "alive": self.alive_count,
            "dead": self.count - self.alive_count,
        }

    async def health_check_one(self, proxy_url: str, timeout: float = 5.0) -> bool:
        """Test if a proxy is alive by connecting to a known host.
        Tries multiple targets for resilience."""
        targets = [
            "https://httpbin.org/ip",
            "https://api.anthropic.com/v1/users/me",
            "https://www.google.com",
        ]
        for target in targets:
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=httpx.Timeout(timeout, connect=timeout),
                ) as client:
                    r = await client.get(target, timeout=timeout)
                    if r.status_code < 500:
                        return True
            except Exception:
                continue
        return False

    async def health_check_all(self, concurrency: int = 20):
        """Test all proxies and update alive set."""
        sem = asyncio.Semaphore(concurrency)

        async def check(proxy: str):
            async with sem:
                alive = await self.health_check_one(proxy)
                if alive:
                    await self.mark_alive(proxy)
                else:
                    await self.mark_dead(proxy)

        await asyncio.gather(*(check(p) for p in self._all), return_exceptions=True)


# Singleton
_pool: Optional[ProxyPool] = None


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        _pool = ProxyPool()
    return _pool


def reload_pool():
    """Reload proxies from disk and rebuild the pool."""
    global _pool
    _pool = ProxyPool()
