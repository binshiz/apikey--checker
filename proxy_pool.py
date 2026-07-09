"""SOCKS5 proxy pool — load, round-robin, health-check."""

import asyncio
import os
import random
from typing import Optional
from urllib.parse import quote, urlsplit

PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "proxies.txt")


def _build_socks5_url(
    host: str,
    port: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[str]:
    host = host.strip()
    port = port.strip()
    if not host or not port:
        return None
    try:
        port_number = int(port)
    except ValueError:
        return None
    if port_number < 1 or port_number > 65535:
        return None

    if username is not None:
        auth = quote(username.strip(), safe="")
        if password is not None:
            auth = f"{auth}:{quote(password.strip(), safe='')}"
        return f"socks5://{auth}@{host}:{port}"
    return f"socks5://{host}:{port}"


def _split_host_port(value: str) -> Optional[tuple[str, str]]:
    if ":" not in value:
        return None
    host, port = value.rsplit(":", 1)
    if not host or not port.isdigit():
        return None
    return host, port


def parse_proxy_line(line: str) -> Optional[str]:
    """Parse common SOCKS5 proxy formats into a URL. Skip comments/empty."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    if "://" in s:
        try:
            parsed = urlsplit(s)
            if parsed.scheme in {"socks5", "socks5h"} and parsed.hostname and parsed.port:
                return s
        except ValueError:
            return None
        return None

    if "@" in s:
        left, right = s.rsplit("@", 1)
        left_host_port = _split_host_port(left)
        right_host_port = _split_host_port(right)

        if left_host_port:
            host, port = left_host_port
            username, sep, password = right.partition(":")
            return _build_socks5_url(host, port, username, password if sep else None)

        if right_host_port:
            host, port = right_host_port
            username, sep, password = left.partition(":")
            return _build_socks5_url(host, port, username, password if sep else None)

        return None

    parts = s.split(":")
    if len(parts) == 4:
        host, port, pwd, user = parts
        # Legacy local format: host:port:password:username
        return _build_socks5_url(host, port, user, pwd)
    elif len(parts) == 2:
        host, port = parts
        return _build_socks5_url(host, port)
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
        import httpx

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
