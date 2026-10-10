# Distributed Bank Transaction System

A client, a gateway (which also coordinates two-phase commit) and three bank servers, each a separate process talking REST over HTTP.

```
client ──► gateway :8000 ──► server 1 :8001  (A1xx)
                         ├──► server 2 :8002  (A2xx)
                         └──► server 3 :8003  (A3xx)
```

An account ID `A<s><nn>` lives on server `s`.

## 1. Setup

Python 3.10+. Run all commands from this folder (`distributed-bank-system`).

```bash
python3 -m venv ../../bank-venv            # keep the venv outside the submission folder
source ../../bank-venv/bin/activate        # Windows: ..\..\bank-venv\Scripts\activate
pip install -r requirements.txt
```

Activate the venv in every new terminal.

## 2. Start the system (terminal 1)

```bash
python launcher.py --fresh
```

This starts the three servers and the gateway with the seed accounts. Ctrl+C stops everything. `python launcher.py` (without `--fresh`) keeps existing data. Process output goes to `logs/`.

## 3. Start the client (terminal 2)

```bash
python client/client.py
```

Every result is printed as `SUCCESS`, `FAILED: <reason>` or `PENDING: <reason>` with the transaction ID.

## 4. Seed accounts

```
ACCOUNTS
```

| Account | Owner | Server | Balance |
|---|---|---|---|
| A101 | Ram | 1 | ₹10,000 |
| A102 | Sita | 1 | ₹15,000 |
| A201 | Mohan | 2 | ₹20,000 |
| A202 | Priya | 2 | ₹25,000 |
| A301 | Arjun | 3 | ₹30,000 |
| A302 | Lakshmi | 3 | ₹35,000 |

Total ₹1,35,000.

## 5. Normal transactions

```
BALANCE A101                     # 10000
DEPOSIT A101 2000                # SUCCESS, 12000
WITHDRAW A101 3000               # SUCCESS, 9000
WITHDRAW A101 15000              # FAILED: Insufficient balance
CREATE A103 Ravi 5000            # new account on server 1
```

## 6. Cross-server transfer

```
TRANSFER A101 A201 5000          # SUCCESS, A101 = 4000, A201 = 25000 (two-phase commit)
TRANSFER A101 A999 5000          # FAILED: Destination account not found
```

## 7. Concurrent transactions

```
PARALLEL WITHDRAW A102 8000 ; WITHDRAW A102 8000    # one SUCCESS, one FAILED
BALANCE A102                                         # 7000, never negative
```

`PARALLEL` sends all the commands at the same instant.

## 8. Failures

Destination server unavailable:

```bash
python launcher.py --kill 3                          # terminal 3
```
```
TRANSFER A101 A301 1000          # FAILED: Destination server unavailable (Server 3)
HEALTH                           # server_3: unreachable
```
```bash
python launcher.py --start 3                         # terminal 3
```

Server crash during a transaction:

```
FAULT 2 on_commit_before_apply crash
TRANSFER A101 A201 1000          # PENDING (debit done, credit waits for Server 2)
HEALTH                           # server_2: unreachable
```
```bash
python launcher.py --start 2                         # terminal 3; recovery runs on startup
```
```
STATUS <txn_id>                  # SUCCESS
BALANCE A201                     # 26000, credited once
```

Other fault points: `on_prepare_before_vote` (crash before voting), `on_request` (`FAULT 2 on_request delay 500`, add `always` to delay every request).

## 9. Transaction log and final balances

```
LOG                              # last 20 records (LOG 10 for the last 10)
HISTORY A101                     # one account's history with the balance after each step
ACCOUNTS                         # final balances; total 131000 = 135000 + 2000 + 5000 (A103) - 3000 - 8000
```

Other commands: `RESET` (back to the seed accounts, clears the log), `HELP`, `EXIT`.

Each step above also exists as a script: `python client/client.py --script ../test-cases/scenarios/<file>.txt`.

## 10. Tests

```bash
pytest ../test-cases -q
python scripts/make_test_report.py    # also writes ../results/
```

The tests start their own servers on ports 9000–9003, so they can run while the system is up.

## 11. Benchmark

With the system running:

```bash
python bench/benchmark.py --sweep     # writes ../performance-results/
python bench/plot.py                  # graphs in ../performance-results/
```

## 12. Clean-up Command

```bash
cd .. && rm -rf distributed-bank-system/data distributed-bank-system/logs && find . -name __pycache__ -prune -exec rm -rf {} + && find . -name .DS_Store -delete
```

## Configuration

Environment variables: `BANK_BASE_PORT` (8000), `BANK_DATA_DIR` (data/), `BANK_LOG_DIR` (logs/), `BANK_CONNECT_TIMEOUT` (1 s), `BANK_READ_TIMEOUT` (3 s), `BANK_RECOVERY_INTERVAL` (2 s), `BANK_IN_DOUBT_TIMEOUT` (10 s), `BANK_THREAD_POOL_SIZE` (100).

If a port is in use, set `BANK_BASE_PORT`. If a process won't start, check its file in `logs/`.
