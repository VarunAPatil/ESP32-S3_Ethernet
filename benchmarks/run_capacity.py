#!/usr/bin/env python3
"""Measure end-to-end throughput through an ESP32-S3 USB NCM interface.

Uses Cloudflare's public speed-test endpoints. Run only when the NCM interface is
connected; all curl and ping traffic is bound to the selected interface.
"""

import argparse
import json
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


COUNTERS = ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
PING_RTT = re.compile(r"time=([0-9.]+) ms")
PING_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) received, ([0-9.]+)% packet loss")
CURL_FORMAT = "%{size_download}\t%{size_upload}\t%{time_total}\t%{speed_download}\t%{speed_upload}\t%{http_code}"


def counters(interface):
    base = Path("/sys/class/net") / interface / "statistics"
    return {name: int((base / name).read_text()) for name in COUNTERS}


def ping_samples(interface, gateway, count=15):
    result = subprocess.run(
        ["ping", "-I", interface, "-n", "-i", "0.2", "-W", "2", "-c", str(count), gateway],
        text=True, capture_output=True, timeout=count * 3 + 5,
    )
    return [float(value) for value in PING_RTT.findall(result.stdout)]


def curl_transfer(interface, direction, size, payload=None, limit_bps=None):
    command = [
        "curl", "--interface", interface, "--fail", "--silent", "--show-error",
        "--max-time", "120", "--output", "/dev/null", "--write-out", CURL_FORMAT,
    ]
    if limit_bps:
        command += ["--limit-rate", str(limit_bps)]
    if direction == "download":
        command.append(f"https://speed.cloudflare.com/__down?bytes={size}")
    else:
        command += ["--data-binary", f"@{payload}", "https://speed.cloudflare.com/__up"]
    result = subprocess.run(command, text=True, capture_output=True, timeout=125)
    if result.returncode:
        raise RuntimeError(f"{direction} failed: {result.stderr.strip()}")
    downloaded, uploaded, elapsed, down_speed, up_speed, status = result.stdout.strip().split("\t")
    if status != "200":
        raise RuntimeError(f"{direction} returned HTTP {status}")
    return {
        "bytes": int(float(downloaded if direction == "download" else uploaded)),
        "seconds": float(elapsed),
        "mbps": 8 * float(down_speed if direction == "download" else up_speed) / 1_000_000,
    }


def loaded_transfer(interface, gateway, direction, size, payload=None, limit_bps=None):
    ping = subprocess.Popen(
        ["ping", "-I", interface, "-n", "-i", "0.2", gateway],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    before = counters(interface)
    started = time.monotonic()
    try:
        transfer = curl_transfer(interface, direction, size, payload, limit_bps)
    finally:
        ping.send_signal(signal.SIGINT)
        stdout, _ = ping.communicate(timeout=5)
    after = counters(interface)
    transfer["wall_seconds"] = round(time.monotonic() - started, 3)
    transfer["gateway_ping_ms"] = [float(value) for value in PING_RTT.findall(stdout)]
    counts = PING_COUNTS.search(stdout)
    if counts:
        transfer["gateway_ping_sent"] = int(counts.group(1))
        transfer["gateway_ping_received"] = int(counts.group(2))
        transfer["gateway_ping_loss_pct"] = float(counts.group(3))
    transfer["counter_delta"] = {key: after[key] - before[key] for key in COUNTERS}
    return transfer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="USB NCM interface, e.g. enxe072a1d47a44")
    parser.add_argument("--gateway", required=True, help="DHCP gateway address")
    parser.add_argument("--output", required=True, type=Path, help="JSON result path")
    parser.add_argument("--size", type=int, default=20_000_000, help="bytes per sustained transfer")
    parser.add_argument("--repeats", type=int, default=2, help="transfers per direction")
    parser.add_argument("--direction", choices=("download", "upload", "both"), default="both")
    parser.add_argument("--limit-bps", type=int, help="optional curl rate limit in bytes/s")
    args = parser.parse_args()

    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "interface": args.interface,
        "gateway": args.gateway,
        "endpoint": "speed.cloudflare.com",
        "size_bytes": args.size,
        "limit_Bps": args.limit_bps,
        "idle_gateway_ping_ms": ping_samples(args.interface, args.gateway),
        "transfers": [],
    }
    print(f"Idle gateway ping: {len(result['idle_gateway_ping_ms'])}/15 replies", flush=True)

    with tempfile.NamedTemporaryFile(prefix="ncm-upload-", suffix=".bin") as payload:
        payload.truncate(args.size)
        payload.flush()
        directions = ("download", "upload") if args.direction == "both" else (args.direction,)
        for direction in directions:
            for attempt in range(1, args.repeats + 1):
                entry = loaded_transfer(args.interface, args.gateway, direction, args.size, payload.name, args.limit_bps)
                entry.update(direction=direction, attempt=attempt)
                result["transfers"].append(entry)
                print(f"{direction} {attempt}: {entry['mbps']:.2f} Mb/s, {entry['seconds']:.1f} s, "
                      f"{len(entry['gateway_ping_ms'])} loaded ping replies", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
