"""Sequential vs concurrent benchmark (start the system first).

    python bench/benchmark.py [--workers 20] [--reps 3] [--sweep] [--net-delay-ms 5]
"""

import argparse
import csv
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

GATEWAY_URL = f"http://{config.GATEWAY_HOST}:{config.GATEWAY_PORT}"
RESULTS_DIR = config.PERF_RESULTS_DIR

ACCOUNTS_PER_SERVER = 10
INITIAL_BALANCE = 100_000

CSV_FIELDS = [
    "mode", "workers", "total", "reps", "successes", "failures",
    "wall_time_s", "throughput_rps", "avg_ms", "p50_ms", "p95_ms", "max_ms", "consistent",
]


def benchmark_accounts() -> list[str]:
    return [f"A{sid}{i:02d}" for sid in config.SERVERS for i in range(1, ACCOUNTS_PER_SERVER + 1)]


def setup_benchmark(client: httpx.Client, accounts: list[str], net_delay_ms: int = 0) -> None:
    """Wipe all servers, create the benchmark accounts, optionally arm a network delay."""
    resp = client.post(f"{GATEWAY_URL}/admin/reset", json={"seed": False}, timeout=10.0)
    resp.raise_for_status()
    if resp.json()["errors"]:
        raise RuntimeError(f"Reset failed: {resp.json()['errors']}")
    for aid in accounts:
        r = client.post(f"{GATEWAY_URL}/accounts", json={
            "account_id": aid, "owner": f"Bench_{aid}", "initial_balance": INITIAL_BALANCE,
        }, timeout=10.0)
        r.raise_for_status()
    if net_delay_ms:
        for url in config.SERVERS.values():
            client.post(f"{url}/admin/fault", json={
                "point": "on_request", "action": "delay", "delay_ms": net_delay_ms, "once": False,
            }, timeout=5.0).raise_for_status()


def generate_workload(accounts: list[str], n: int, seed: int = 42) -> list[dict]:
    """Generate a mix of operations: 30% deposit, 20% withdraw, 20% balance, 30% transfer."""
    rng = random.Random(seed)
    ops = []
    for _ in range(n):
        r = rng.random()
        acct = rng.choice(accounts)
        if r < 0.3:
            ops.append({"op": "deposit", "account_id": acct, "amount": rng.randint(100, 5000)})
        elif r < 0.5:
            ops.append({"op": "withdraw", "account_id": acct, "amount": rng.randint(100, 2000)})
        elif r < 0.7:
            ops.append({"op": "balance", "account_id": acct})
        else:
            dst = rng.choice([a for a in accounts if a != acct])
            ops.append({"op": "transfer", "source": acct, "destination": dst,
                        "amount": rng.randint(100, 3000)})
    return ops


def execute_op(client: httpx.Client, op: dict) -> tuple[float, int]:
    """Execute a single operation. Returns (latency_ms, http_status); status 0 = transport error."""
    start = time.perf_counter()
    try:
        if op["op"] == "deposit":
            resp = client.post(f"{GATEWAY_URL}/accounts/{op['account_id']}/deposit",
                               json={"amount": op["amount"]}, timeout=10.0)
        elif op["op"] == "withdraw":
            resp = client.post(f"{GATEWAY_URL}/accounts/{op['account_id']}/withdraw",
                               json={"amount": op["amount"]}, timeout=10.0)
        elif op["op"] == "balance":
            resp = client.get(f"{GATEWAY_URL}/accounts/{op['account_id']}/balance", timeout=10.0)
        else:
            resp = client.post(f"{GATEWAY_URL}/transfers", json={
                "source": op["source"], "destination": op["destination"], "amount": op["amount"],
            }, timeout=10.0)
        status = resp.status_code
    except httpx.TransportError:
        status = 0
    return (time.perf_counter() - start) * 1000, status


def run_workload(client: httpx.Client, ops: list[dict], workers: int) -> tuple[dict, list[int]]:
    """Run ops with the given number of workers (1 = sequential). Returns (stats, statuses)."""
    wall_start = time.perf_counter()
    if workers == 1:
        results = [execute_op(client, op) for op in ops]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda op: execute_op(client, op), ops))
    wall_time = time.perf_counter() - wall_start

    latencies = sorted(lat for lat, _ in results)
    statuses = [st for _, st in results]
    successes = sum(1 for st in statuses if st == 200)
    stats = {
        "total": len(ops),
        "successes": successes,
        "failures": len(ops) - successes,
        "wall_time_s": wall_time,
        "throughput_rps": len(ops) / wall_time if wall_time > 0 else 0,
        "avg_ms": statistics.fmean(latencies),
        "p50_ms": latencies[len(latencies) // 2],
        "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
        "max_ms": latencies[-1],
    }
    return stats, statuses


def check_consistency(client: httpx.Client, accounts: list[str], ops: list[dict],
                      statuses: list[int]) -> tuple[bool, str]:
    """Total money = initial + deposits − withdrawals; no negative balance; no holds."""
    expected = INITIAL_BALANCE * len(accounts)
    for op, st in zip(ops, statuses):
        if st == 200 and op["op"] == "deposit":
            expected += op["amount"]
        elif st == 200 and op["op"] == "withdraw":
            expected -= op["amount"]

    total = held = 0
    negatives = []
    for aid in accounts:
        data = client.get(f"{GATEWAY_URL}/accounts/{aid}/balance", timeout=10.0).json()["data"]
        total += data["balance"]
        held += data["held"]
        if data["balance"] < 0:
            negatives.append(aid)

    ok = total == expected and held == 0 and not negatives and 0 not in statuses
    detail = f"total ₹{total} (expected ₹{expected}), held ₹{held}, negative: {negatives or 'none'}"
    if 0 in statuses:
        detail += f", transport errors: {statuses.count(0)}"
    return ok, detail


def measure(client: httpx.Client, accounts: list[str], ops: list[dict], workers: int,
            mode: str, reps: int, net_delay_ms: int) -> dict:
    """Run the same workload `reps` times from the same start state and average the stats."""
    runs = []
    consistent = True
    for rep in range(reps):
        setup_benchmark(client, accounts, net_delay_ms)
        stats, statuses = run_workload(client, ops, workers)
        ok, detail = check_consistency(client, accounts, ops, statuses)
        consistent &= ok
        runs.append(stats)
        print(f"    rep {rep + 1}: {stats['throughput_rps']:.1f} req/s, avg {stats['avg_ms']:.2f} ms, "
              f"{stats['successes']} ok / {stats['failures']} failed; "
              f"consistency {'OK' if ok else 'VIOLATED'}: {detail}")

    avg = {k: statistics.fmean(r[k] for r in runs) for k in runs[0]}
    return {
        "mode": mode,
        "workers": workers,
        "total": len(ops),
        "reps": reps,
        "successes": round(avg["successes"], 1),
        "failures": round(avg["failures"], 1),
        "wall_time_s": round(avg["wall_time_s"], 3),
        "throughput_rps": round(avg["throughput_rps"], 1),
        "avg_ms": round(avg["avg_ms"], 2),
        "p50_ms": round(avg["p50_ms"], 2),
        "p95_ms": round(avg["p95_ms"], 2),
        "max_ms": round(avg["max_ms"], 2),
        "consistent": consistent,
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent workers")
    parser.add_argument("--requests", type=int, nargs="*", default=[10, 50, 100, 500, 1000],
                        help="Request counts to benchmark")
    parser.add_argument("--sweep", action="store_true",
                        help="Also run 1000 requests with 1, 5, 10, 20, 50 workers")
    parser.add_argument("--reps", type=int, default=3, help="Repetitions per workload (averaged)")
    parser.add_argument("--net-delay-ms", type=int, default=0,
                        help="Server-side delay per request to emulate network latency")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=15.0, limits=httpx.Limits(max_connections=100))
    accounts = benchmark_accounts()
    print(f"Benchmark: {len(accounts)} accounts, {args.reps} reps per run, "
          f"net delay {args.net_delay_ms} ms\n")

    results = []
    for n in args.requests:
        ops = generate_workload(accounts, n)
        print(f"--- {n} requests ---")

        print("  Sequential (1 thread):")
        seq = measure(client, accounts, ops, 1, "sequential", args.reps, args.net_delay_ms)
        results.append(seq)

        print(f"  Concurrent ({args.workers} workers):")
        conc = measure(client, accounts, ops, args.workers, "concurrent", args.reps, args.net_delay_ms)
        results.append(conc)

        print(f"  → sequential {seq['throughput_rps']} req/s, avg {seq['avg_ms']} ms | "
              f"concurrent {conc['throughput_rps']} req/s, avg {conc['avg_ms']} ms\n")

    if args.sweep:
        n = 1000
        print(f"--- Worker sweep ({n} requests) ---")
        ops = generate_workload(accounts, n)
        for w in [1, 5, 10, 20, 50]:
            print(f"  {w} workers:")
            results.append(measure(client, accounts, ops, w, "sweep", args.reps, args.net_delay_ms))

    # Leave the system in its normal seeded state
    client.post(f"{GATEWAY_URL}/admin/reset", json={"seed": True}, timeout=10.0)

    csv_path = RESULTS_DIR / "perf_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    md_path = RESULTS_DIR / "perf_table.md"
    with open(md_path, "w") as f:
        f.write("# Performance Results\n\n")
        f.write(f"{len(accounts)} accounts across {len(config.SERVERS)} servers; "
                f"mix 30% deposit / 20% withdraw / 20% balance / 30% transfer; "
                f"each row is the mean of {args.reps} runs; network delay {args.net_delay_ms} ms.\n\n")
        f.write("| Mode | Workers | Requests | Throughput (req/s) | Avg (ms) | P50 (ms) | P95 (ms) "
                "| Max (ms) | Success | Failed | Consistent |\n")
        f.write("|------|---------|----------|--------------------|----------|----------|----------"
                "|----------|---------|--------|------------|\n")
        for r in results:
            f.write(f"| {r['mode']} | {r['workers']} | {r['total']} | {r['throughput_rps']} | "
                    f"{r['avg_ms']} | {r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} | "
                    f"{r['successes']} | {r['failures']} | {'✅' if r['consistent'] else '❌'} |\n")

    print(f"Results saved to {csv_path} and {md_path}")
    print("Run `python bench/plot.py` to generate graphs.")
    client.close()

    if not all(r["consistent"] for r in results):
        print("WARNING: a consistency check failed — see the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
