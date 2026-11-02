#!/usr/bin/env python3
"""Validate direct ESP32-S3 SoftAP -> laptop Wi-Fi transmit throughput.

Requires iperf 2 on the laptop and the ESP-IDF Wi-Fi iperf example on the S3.
The script refuses to run unless the laptop is on the benchmark SoftAP and its
route to 192.168.4.1 uses the selected Wi-Fi interface.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def verify_path(interface, ssid, esp_ip):
    active = command("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface)
    if active != ssid:
        raise RuntimeError(f"{interface} is connected to {active!r}, expected {ssid!r}")
    route = command("ip", "route", "get", esp_ip)
    if not re.search(rf"\bdev {re.escape(interface)}\b", route):
        raise RuntimeError(f"Route to ESP does not use {interface}: {route}")
    match = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)\b", route)
    if not match:
        raise RuntimeError(f"No laptop source address in route: {route}")
    local_ip = match.group(1)
    if not local_ip.startswith("192.168.4.") or esp_ip != "192.168.4.1":
        raise RuntimeError(f"Expected direct ESP SoftAP subnet; found {local_ip} -> {esp_ip}")
    return local_ip, route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="wlp8s0")
    parser.add_argument("--ssid", default="S3-TX-BENCH")
    parser.add_argument("--esp-ip", default="192.168.4.1")
    parser.add_argument("--iperf", default=shutil.which("iperf") or "/tmp/esp32s3-iperf2/usr/bin/iperf")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--mode", choices=["tcp", "udp"], default="tcp")
    parser.add_argument("--bandwidth", type=int, default=20, help="ESP UDP offered rate in Mb/s")
    parser.add_argument("--output", type=Path, help="Save received iperf output and path checks as JSON")
    args = parser.parse_args()
    if args.seconds < 1 or args.bandwidth < 1:
        parser.error("seconds and bandwidth must be positive")
    try:
        # iperf 2.1.5 prints its version but exits with status 1.
        version_result = subprocess.run([args.iperf, "--version"], text=True, capture_output=True)
        version = (version_result.stdout + version_result.stderr).strip()
        if "iperf version 2" not in version:
            raise RuntimeError(f"Use iperf 2, not iperf 3: {version}")
        local_ip, route = verify_path(args.interface, args.ssid, args.esp_ip)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        parser.exit(2, f"Validation failed: {exc}\n")

    # Recheck after the optional socket probe, keeping an unambiguous result.
    local_ip, route = verify_path(args.interface, args.ssid, args.esp_ip)
    esp_command = f"iperf {'-u ' if args.mode == 'udp' else ''}-c {local_ip} "
    if args.mode == "udp":
        esp_command += f"-b {args.bandwidth} -l 1400 "
    esp_command += f"-i 1 -t {args.seconds}"
    print(f"Direct path verified: ESP {args.esp_ip} -> {args.interface} {local_ip} ({args.ssid})", flush=True)
    print(f"Run on ESP console now: {esp_command}", flush=True)
    print("Waiting for ESP traffic...", flush=True)
    try:
        result = subprocess.run(
            [args.iperf, "-s", "-B", local_ip, "-i", "1"] + (["-u"] if args.mode == "udp" else []),
            text=True, capture_output=True, timeout=args.seconds + 45,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        error = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = subprocess.CompletedProcess([], 0, output, error)
    if not re.search(r"\[\s*\d+\]\s+\d+(?:\.\d+)?-\s*\d+(?:\.\d+)?\s+sec\s+.*(?:bits|Bytes)/sec", result.stdout):
        parser.exit(2, "No completed iperf receiver interval found; ESP Wi-Fi TX is unverified.\n")
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if args.output:
        data = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "chip": "ESP32-S3", "direction": "ESP Wi-Fi TX to laptop", "mode": args.mode,
            "ssid": args.ssid, "interface": args.interface, "esp_ip": args.esp_ip,
            "laptop_ip": local_ip, "route": route, "iperf_version": version,
            "esp_command": esp_command, "server_output": result.stdout,
            "server_stderr": result.stderr, "server_exit_code": result.returncode,
        }
        args.output.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Saved {args.output}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
