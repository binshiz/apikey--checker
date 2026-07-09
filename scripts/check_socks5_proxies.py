#!/usr/bin/env python3
"""Batch-check SOCKS5 proxies from data/proxies.txt."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from proxy_pool import PROXIES_FILE, parse_proxy_line  # noqa: E402

DEFAULT_TARGETS = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://www.cloudflare.com/cdn-cgi/trace",
]


@dataclass(frozen=True)
class ProxyEntry:
    line_no: int
    raw: str
    url: str


@dataclass(frozen=True)
class ProxyResult:
    entry: ProxyEntry
    alive: bool
    elapsed: float
    target: Optional[str] = None
    status_code: Optional[int] = None
    ip: Optional[str] = None
    error: Optional[str] = None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check SOCKS5 proxy availability and export working proxies.",
    )
    parser.add_argument(
        "proxy_file",
        nargs="?",
        default=PROXIES_FILE,
        help="Proxy list file. Default: data/proxies.txt",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(Path(PROXIES_FILE).with_name("working_proxies.txt")),
        help="Where to write working proxies. Use '' to disable. Default: data/working_proxies.txt",
    )
    parser.add_argument(
        "--dead-output",
        default="",
        help="Optional file to write failed proxies.",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=20,
        help="Number of proxies to check at once. Default: 20",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=8.0,
        help="Per-target timeout in seconds. Default: 8",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="HTTP(S) URL to probe. Can be repeated. Defaults to several IP echo endpoints.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only check the first N parsed proxies. Default: all",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the final summary.",
    )
    parser.add_argument(
        "--fail-on-dead",
        action="store_true",
        help="Exit with code 1 when any checked proxy fails.",
    )
    return parser.parse_args(argv)


def load_entries(proxy_file: Path, limit: int = 0) -> tuple[list[ProxyEntry], list[tuple[int, str]]]:
    entries: list[ProxyEntry] = []
    invalid: list[tuple[int, str]] = []

    with proxy_file.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue

            url = parse_proxy_line(raw)
            if not url:
                invalid.append((line_no, raw))
                continue

            entries.append(ProxyEntry(line_no=line_no, raw=raw, url=url))
            if limit and len(entries) >= limit:
                break

    return entries, invalid


def ensure_socks_dependencies() -> bool:
    try:
        import httpx  # noqa: F401
        import socksio  # noqa: F401
    except ModuleNotFoundError as exc:
        missing = exc.name or "dependency"
        print(
            f"Missing dependency: {missing}. Run `python3 -m pip install -r requirements.txt` first.",
            file=sys.stderr,
        )
        return False
    return True


def mask_proxy_url(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    host_port = f"{host}:{parsed.port}" if parsed.port else host
    if parsed.username:
        return f"{parsed.scheme}://{parsed.username}:***@{host_port}"
    return f"{parsed.scheme}://{host_port}"


def extract_ip(response) -> Optional[str]:
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()

    if "json" in content_type:
        try:
            data = response.json()
        except json.JSONDecodeError:
            return None
        ip_value = data.get("ip") or data.get("origin")
        return str(ip_value) if ip_value else None

    for line in text.splitlines():
        if line.startswith("ip="):
            return line.partition("=")[2].strip()

    return None


def short_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


async def check_one(entry: ProxyEntry, targets: Iterable[str], timeout: float) -> ProxyResult:
    import httpx

    started = time.perf_counter()
    last_error = "no targets configured"

    for target in targets:
        try:
            async with httpx.AsyncClient(
                proxy=entry.url,
                timeout=httpx.Timeout(timeout, connect=timeout),
                follow_redirects=True,
                headers={"user-agent": "apikey-checker-proxy-test/1.0"},
            ) as client:
                response = await client.get(target)

            elapsed = time.perf_counter() - started
            if response.status_code < 500:
                return ProxyResult(
                    entry=entry,
                    alive=True,
                    elapsed=elapsed,
                    target=target,
                    status_code=response.status_code,
                    ip=extract_ip(response),
                )
            last_error = f"HTTP {response.status_code} from {target}"
        except Exception as exc:
            last_error = short_error(exc)

    return ProxyResult(
        entry=entry,
        alive=False,
        elapsed=time.perf_counter() - started,
        error=last_error,
    )


def print_result(index: int, total: int, result: ProxyResult) -> None:
    label = "OK" if result.alive else "BAD"
    detail = result.error or f"HTTP {result.status_code}"
    if result.target:
        detail = f"{detail} via {result.target}"
    if result.ip:
        detail = f"{detail} ip={result.ip}"
    print(
        f"[{index:>4}/{total:<4}] {label:<3} "
        f"{result.elapsed:>6.2f}s  {mask_proxy_url(result.entry.url):<42}  {detail}",
        flush=True,
    )


async def check_all(
    entries: list[ProxyEntry],
    targets: list[str],
    concurrency: int,
    timeout: float,
    quiet: bool,
) -> list[ProxyResult]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(entry: ProxyEntry) -> ProxyResult:
        async with semaphore:
            return await check_one(entry, targets, timeout)

    tasks = [asyncio.create_task(run(entry)) for entry in entries]
    results: list[ProxyResult] = []
    total = len(tasks)

    for index, task in enumerate(asyncio.as_completed(tasks), start=1):
        result = await task
        results.append(result)
        if not quiet:
            print_result(index, total, result)

    return results


def write_proxy_file(path: str, results: list[ProxyResult], alive: bool) -> None:
    if not path:
        return
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected = [
        result.entry.raw
        for result in sorted(results, key=lambda item: item.entry.line_no)
        if result.alive is alive
    ]
    output_path.write_text("\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    proxy_file = Path(args.proxy_file).expanduser()
    targets = args.targets or DEFAULT_TARGETS

    if not proxy_file.exists():
        print(f"Proxy file not found: {proxy_file}", file=sys.stderr)
        return 2

    entries, invalid = load_entries(proxy_file, args.limit)
    if not entries:
        print(f"No valid proxies found in {proxy_file}", file=sys.stderr)
        return 2

    if not ensure_socks_dependencies():
        return 2

    print(
        f"Loaded {len(entries)} proxies from {proxy_file}"
        + (f" ({len(invalid)} invalid skipped)" if invalid else "")
    )
    print(f"Targets: {', '.join(targets)}")

    results = asyncio.run(
        check_all(
            entries=entries,
            targets=targets,
            concurrency=args.concurrency,
            timeout=args.timeout,
            quiet=args.quiet,
        )
    )
    alive_count = sum(1 for result in results if result.alive)
    dead_count = len(results) - alive_count

    write_proxy_file(args.output, results, alive=True)
    write_proxy_file(args.dead_output, results, alive=False)

    if args.output:
        print(f"Working proxies written to {args.output}")
    if args.dead_output:
        print(f"Failed proxies written to {args.dead_output}")
    print(f"Summary: {alive_count} alive, {dead_count} dead, {len(results)} checked")

    return 1 if args.fail_on_dead and dead_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
