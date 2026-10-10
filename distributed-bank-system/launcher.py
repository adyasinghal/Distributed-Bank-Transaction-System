"""Starts the three bank servers and the gateway.

    python launcher.py [--fresh] [--crash-at 2:on_commit_before_apply]
    python launcher.py --kill 2      (from another terminal)
    python launcher.py --start 2
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import shutil
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config


def wait_for_health(url: str, timeout: float = 10.0) -> bool:
    """Wait until a /health endpoint responds with 200."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{url}/health", timeout=1.0)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _spawn(cmd: list[str], log_name: str) -> subprocess.Popen:
    """Start a child process with its output appended to logs/<log_name>."""
    config.ensure_dirs()
    log_file = open(config.LOG_DIR / log_name, "a")
    return subprocess.Popen(
        cmd,
        cwd=str(config.PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def start_server(server_id: int) -> subprocess.Popen:
    """Start a bank server subprocess."""
    port = config.SERVER_PORTS[server_id]
    cmd = [
        sys.executable, "-m", "server.bank_server",
        "--id", str(server_id),
        "--port", str(port),
    ]
    return _spawn(cmd, f"server{server_id}.out")


def start_gateway() -> subprocess.Popen:
    """Start the gateway subprocess."""
    cmd = [
        sys.executable, "-m", "gateway.gateway",
        "--port", str(config.GATEWAY_PORT),
    ]
    return _spawn(cmd, "gateway.out")


def pid_file(server_id: int) -> Path:
    return config.DATA_DIR / f"server{server_id}.pid"


def arm_crash(spec: str) -> None:
    """Arm a one-shot crash fault, spec = '<server_id>:<point>'."""
    sid_str, _, point = spec.partition(":")
    sid = int(sid_str)
    resp = httpx.post(f"{config.SERVERS[sid]}/admin/fault", json={
        "point": point, "action": "crash", "delay_ms": 0, "once": True,
    }, timeout=3.0)
    if resp.status_code == 200:
        print(f"  Fault armed: Server {sid} will crash at {point}")
    else:
        print(f"  Warning: could not arm {spec}: HTTP {resp.status_code} {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="Distributed Bank Launcher")
    parser.add_argument("--fresh", action="store_true", help="Wipe data directory and re-seed")
    parser.add_argument("--kill", type=int, metavar="SERVER_ID", help="Kill a running server")
    parser.add_argument("--start", type=int, metavar="SERVER_ID", help="Start/restart a server")
    parser.add_argument("--crash-at", action="append", default=[], metavar="SID:POINT",
                        help="Arm a crash fault after startup, e.g. 2:on_commit_before_apply")
    args = parser.parse_args()

    # Kill mode
    if args.kill is not None:
        pf = pid_file(args.kill)
        if pf.exists():
            pid = int(pf.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"Killed Server {args.kill} (PID {pid})")
            except ProcessLookupError:
                print(f"Server {args.kill} (PID {pid}) is not running")
            pf.unlink(missing_ok=True)
        else:
            print(f"No PID file found for Server {args.kill}")
        return

    # Start mode (single server)
    if args.start is not None:
        url = config.SERVERS[args.start]
        if wait_for_health(url, timeout=0.5):
            print(f"Server {args.start} is already running on port {config.SERVER_PORTS[args.start]}")
            return
        proc = start_server(args.start)
        pid_file(args.start).write_text(str(proc.pid))
        time.sleep(0.5)
        if proc.poll() is not None:
            print(f"Server {args.start} exited immediately (see logs/server{args.start}.out)")
        elif wait_for_health(url):
            print(f"Server {args.start} started (PID {proc.pid}, port {config.SERVER_PORTS[args.start]})")
        else:
            print(f"Server {args.start} started but health check failed "
                  f"(see logs/server{args.start}.out)")
        return

    # Full startup — refuse if a cluster is already up: --fresh would wipe its
    # data underneath it, and our health checks would be answered by the old one
    running = [name for name, url in
               [(f"Server {sid}", u) for sid, u in config.SERVERS.items()]
               + [("Gateway", f"http://{config.GATEWAY_HOST}:{config.GATEWAY_PORT}")]
               if wait_for_health(url, timeout=0.3)]
    if running:
        print(f"Already running: {', '.join(running)}. Stop the other launcher first "
              f"(Ctrl+C in its terminal), or use --kill/--start for single servers.")
        sys.exit(1)

    # Closing the terminal or `kill <launcher pid>` should stop the cluster too
    def _shutdown_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _shutdown_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _shutdown_signal)

    if args.fresh:
        if config.DATA_DIR.exists():
            shutil.rmtree(config.DATA_DIR)
        if config.LOG_DIR.exists():
            shutil.rmtree(config.LOG_DIR)
        print("Data and logs wiped.")

    config.ensure_dirs()
    procs = {}

    print("Starting bank servers...")
    for sid in config.SERVER_PORTS:
        proc = start_server(sid)
        procs[f"Server {sid}"] = proc
        pid_file(sid).write_text(str(proc.pid))
        print(f"  Server {sid}: PID {proc.pid}, port {config.SERVER_PORTS[sid]}")

    # Wait for servers to be ready
    for sid, url in config.SERVERS.items():
        if wait_for_health(url, timeout=8):
            print(f"  Server {sid}: ✓ healthy")
        else:
            print(f"  Server {sid}: ✗ not responding (see logs/server{sid}.out)")

    print("Starting gateway...")
    gw_proc = start_gateway()
    procs["Gateway"] = gw_proc
    gw_url = f"http://{config.GATEWAY_HOST}:{config.GATEWAY_PORT}"
    if wait_for_health(gw_url, timeout=8):
        print(f"  Gateway: ✓ healthy (port {config.GATEWAY_PORT})")
    else:
        print("  Gateway: ✗ not responding (see logs/gateway.out)")

    for spec in args.crash_at:
        arm_crash(spec)

    print("\n" + "=" * 50)
    print("System is running. Press Ctrl+C to stop all.")
    print(f"Gateway: {gw_url}")
    print("Client:  python client/client.py")
    print(f"Logs:    {config.LOG_DIR}")
    print("=" * 50)

    reported_dead = set()
    try:
        # Wait for Ctrl+C, reporting processes that exit (crash fault or --kill)
        while True:
            time.sleep(1)
            for name, proc in procs.items():
                if proc.poll() is not None and name not in reported_dead:
                    reported_dead.add(name)
                    print(f"[launcher] {name} exited (code {proc.returncode})")
    except KeyboardInterrupt:
        print("\nShutting down...")
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        # Servers restarted with --start are not our children; stop them via PID files
        own_pids = {p.pid for p in procs.values()}
        for sid in config.SERVER_PORTS:
            pf = pid_file(sid)
            if pf.exists():
                pid = int(pf.read_text().strip())
                if pid not in own_pids:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                pf.unlink(missing_ok=True)
        for proc in procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("All processes stopped.")


if __name__ == "__main__":
    main()
