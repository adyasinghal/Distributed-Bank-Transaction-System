"""Plots perf_results.csv from the performance results folder."""

import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

RESULTS_DIR = config.PERF_RESULTS_DIR


def load_csv() -> list[dict]:
    csv_path = RESULTS_DIR / "perf_results.csv"
    if not csv_path.exists():
        print(f"No results file found at {csv_path}. Run benchmark.py first.")
        sys.exit(1)
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def plot_response_time(data: list[dict]):
    """Bar chart: avg response time — sequential vs concurrent by request count."""
    seq = [r for r in data if r["mode"] == "sequential"]
    conc = [r for r in data if r["mode"] == "concurrent"]

    if not seq or not conc:
        return

    conc_by_n = {r["total"]: r for r in conc}
    seq = [r for r in seq if r["total"] in conc_by_n]
    labels = [r["total"] for r in seq]
    seq_vals = [float(r["avg_ms"]) for r in seq]
    conc_vals = [float(conc_by_n[r["total"]]["avg_ms"]) for r in seq]

    x = range(len(labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar([i - width/2 for i in x], seq_vals, width, label="Sequential", color="#2196F3")
    ax.bar([i + width/2 for i in x], conc_vals, width, label=f"Concurrent ({conc[0]['workers']} workers)", color="#FF9800")
    ax.set_xlabel("Number of Requests")
    ax.set_ylabel("Average Response Time (ms)")
    ax.set_title("Average Response Time: Sequential vs Concurrent")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "response_time.png", dpi=150)
    print("Saved response_time.png")


def plot_throughput(data: list[dict]):
    """Bar chart: throughput — sequential vs concurrent by request count."""
    seq = [r for r in data if r["mode"] == "sequential"]
    conc = [r for r in data if r["mode"] == "concurrent"]

    if not seq or not conc:
        return

    conc_by_n = {r["total"]: r for r in conc}
    seq = [r for r in seq if r["total"] in conc_by_n]
    labels = [r["total"] for r in seq]
    seq_vals = [float(r["throughput_rps"]) for r in seq]
    conc_vals = [float(conc_by_n[r["total"]]["throughput_rps"]) for r in seq]

    x = range(len(labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar([i - width/2 for i in x], seq_vals, width, label="Sequential", color="#4CAF50")
    ax.bar([i + width/2 for i in x], conc_vals, width, label=f"Concurrent ({conc[0]['workers']} workers)", color="#E91E63")
    ax.set_xlabel("Number of Requests")
    ax.set_ylabel("Throughput (requests/second)")
    ax.set_title("Throughput: Sequential vs Concurrent")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "throughput.png", dpi=150)
    print("Saved throughput.png")


def plot_worker_sweep(data: list[dict]):
    """Line chart: throughput vs worker count."""
    sweep = [r for r in data if r["mode"] == "sweep"]
    if not sweep:
        return

    workers = [int(r["workers"]) for r in sweep]
    throughput = [float(r["throughput_rps"]) for r in sweep]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(workers, throughput, "o-", color="#9C27B0", linewidth=2, markersize=8)
    ax.set_xlabel("Number of Workers")
    ax.set_ylabel("Throughput (requests/second)")
    ax.set_title(f"Throughput vs Worker Count ({sweep[0]['total']} requests)")
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "workers_sweep.png", dpi=150)
    print("Saved workers_sweep.png")


def main():
    data = load_csv()
    plot_response_time(data)
    plot_throughput(data)
    plot_worker_sweep(data)
    print(f"All graphs saved to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
