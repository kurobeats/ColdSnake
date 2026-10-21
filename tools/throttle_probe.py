"""Measure the Icedrive control-request throttle before designing a scrub pass.

A scrub needs one listing call per remote folder, so the real question is how
many listing calls per second the account tolerates before it answers 429. That
number has never been measured - the code only learned to survive 429 - and it
decides the scrub size (folders/night) and the client pacer default.

This tool ramps the call rate on one folder and reports where 429 starts,
whether Retry-After accompanies it, and how long recovery takes.

It bypasses Client._retry deliberately: that layer sleeps 60 s on a 429, which
would hide the exact signal being measured.

    python -m tools.throttle_probe --self-check          # no network
    python -m tools.throttle_probe --folder-id 12345     # measure

Measurement only: no uploads, no writes, one folder listed repeatedly.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error

from coldsnake.cli import (DEFAULT_CONFIG, TOKEN_CACHE, build_client, load_config,
                           resolve_credentials, resolve_device_id)
from coldsnake.client import API, Client


class _PayloadThrottle(Exception):
    """HTTP 200 carrying an error document - the API's other way of saying 429."""


def rate_at(calls: int, elapsed: float) -> float:
    return calls / elapsed if elapsed > 0 else 0.0


def classify(exc: urllib.error.HTTPError) -> dict:
    """What a failed listing attempt tells us about the throttle."""
    retry_after = (exc.headers or {}).get("Retry-After", "")
    return {"status": exc.code,
            "retry_after_s": int(retry_after) if str(retry_after).isdigit() else None}


def probe(client: Client, folder_id: int, max_calls: int, start: float, floor: float,
          max_seconds: float, concurrency: int = 1, ramp_factor: float = 0.7,
          verbose=print) -> dict:
    """Ramp the aggregate call rate with `concurrency` workers; report the first 429.

    Sequential calls cannot exceed 1/latency (~2/s here, measured), which is below
    the throttle, so concurrency is what actually probes the ceiling. A ramp thread
    shrinks the shared interval every 3 s until `floor`.
    """
    url = f"{API}/collection?type=cloud&folderId={folder_id}"
    lock = threading.Lock()
    stop = threading.Event()
    state = {"interval": start}
    throttled: list[dict] = []
    latencies: list[float] = []
    issued = 0
    t0 = time.monotonic()

    def worker() -> None:
        nonlocal issued
        while not stop.is_set():
            with lock:
                if issued >= max_calls:
                    stop.set()
                    return
                issued += 1
                interval = state["interval"]
            time.sleep(interval)
            started = time.monotonic()
            try:
                payload = client._request(url)        # noqa: SLF001 - measuring raw responses
                # Icedrive reports errors as HTTP 200 + {"error": true}: a payload
                # throttle would otherwise be counted as a successful call.
                if isinstance(payload, dict) and payload.get("error"):
                    payload = {"status": payload.get("code"),
                               "message": payload.get("message"),
                               "retry_after_s": None}
                    throttle_payload = payload
                    raise _PayloadThrottle(throttle_payload)
            except _PayloadThrottle as exc:
                info = exc.args[0]
                with lock:
                    throttled.append({"call": issued, "elapsed_s": round(started - t0, 1),
                                      "rate_calls_s": round(rate_at(len(latencies), started - t0), 2),
                                      **info})
                verbose(f"error payload after {len(latencies)} ok calls: {info}")
                stop.set()
                return
            except urllib.error.HTTPError as exc:
                info = classify(exc)
                with lock:
                    throttled.append({"call": issued, "elapsed_s": round(started - t0, 1),
                                      "rate_calls_s": round(rate_at(len(latencies), started - t0), 2),
                                      **info})
                verbose(f"429 after {len(latencies)} ok calls "
                        f"({rate_at(len(latencies), started - t0):.2f} calls/s): "
                        f"Retry-After={info['retry_after_s']}")
                stop.set()
                return
            with lock:
                latencies.append(time.monotonic() - started)
                if len(latencies) % 100 == 0:
                    recent = latencies[-50:]
                    verbose(f"{len(latencies)} calls, {rate_at(len(latencies), started - t0):.2f} calls/s, "
                            f"last-50 median {statistics.median(recent):.2f}s")
            if time.monotonic() - t0 > max_seconds:
                stop.set()

    def ramp() -> None:
        while not stop.is_set():
            time.sleep(3)
            with lock:
                state["interval"] = max(floor, state["interval"] * ramp_factor)
                interval = state["interval"]
            if ramp_factor < 1:
                verbose(f"ramp: interval {interval:.3f}s x{concurrency} workers "
                        f"= {concurrency / interval:.1f} calls/s offered")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    ramper = threading.Thread(target=ramp, daemon=True)
    ramper.start()
    for thread in threads:
        thread.join()
    stop.set()
    ramper.join(timeout=1)

    elapsed = time.monotonic() - t0
    calls = len(latencies)
    ordered = sorted(latencies)
    result = {"folder_id": folder_id, "concurrency": concurrency, "calls": calls,
              "elapsed_s": round(elapsed, 1),
              "sustained_calls_s": round(rate_at(calls, elapsed), 2),
              "latency_s": {"median": round(statistics.median(ordered), 3) if ordered else None,
                            "p95": round(ordered[int(len(ordered) * 0.95)], 3) if ordered else None},
              "throttled": throttled}
    result.update(recover(client, url, verbose) if throttled else {"recovered_s": None})
    return result


def recover(client: Client, url: str, verbose=print) -> dict:
    """How long after a 429 a listing succeeds again (the cooldown the pacer must respect)."""
    t0 = time.monotonic()
    for wait in (1, 2, 4, 8, 15, 30, 30):
        time.sleep(wait)
        try:
            client._request(url)                       # noqa: SLF001
            took = round(time.monotonic() - t0, 1)
            verbose(f"recovered after {took}s")
            return {"recovered_s": took}
        except urllib.error.HTTPError:
            continue
    verbose("still throttled after the cooldown budget")
    return {"recovered_s": None}


def self_check() -> None:
    """The ramp maths is the only logic here; prove it without touching the network."""
    assert rate_at(10, 2) == 5.0 and rate_at(0, 0) == 0.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder-id", type=int, help="folder to list repeatedly (default: first root folder)")
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--start-interval", type=float, default=1.0, help="seconds between calls at the start")
    parser.add_argument("--floor", type=float, default=0.02, help="fastest interval the ramp reaches")
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=1, help="parallel listing workers")
    parser.add_argument("--ramp-factor", type=float, default=0.7,
                        help="interval shrink per 3s step; 1.0 holds a fixed offered rate")
    parser.add_argument("--self-check", action="store_true", help="run the offline assertions and exit")
    args = parser.parse_args(argv)
    if args.self_check:
        self_check()
        print("self-check ok")
        return 0

    config = load_config(DEFAULT_CONFIG)
    email, password = resolve_credentials(config)
    # control_interval=0: the probe measures the raw transport, so it must not be
    # paced by the production limiter it exists to calibrate.
    client = Client(email, password, verbose=False, device_id=resolve_device_id(config),
                    control_interval=0)
    client._token_path = TOKEN_CACHE                  # noqa: SLF001 - reuse the cached bearer token
    if not client.load_token(TOKEN_CACHE):
        client.login()
        print("logged in fresh (the login itself is not part of the measurement)")

    folder_id = args.folder_id
    if folder_id is None:
        folder_id = next((e["id"] for e in client.listing(0) if e.get("isFolder")), None)
        if folder_id is None:
            print("no folder at the drive root to list; pass --folder-id")
            return 1
    print(f"ramping listing calls on folder {folder_id}: "
          f"{args.start_interval:.2f}s -> {args.floor:.2f}s, cap {args.max_calls} calls")
    print(json.dumps(probe(client, folder_id, args.max_calls, args.start_interval,
                           args.floor, args.max_seconds, args.concurrency,
                           args.ramp_factor), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
