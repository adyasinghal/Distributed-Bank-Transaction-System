# Test Results Report

- **Run date:** 2026-10-10T19:04:26
- **Python:** 3.14.7 (CPython)
- **Platform:** macOS-27.0.1-arm64-arm-64bit-Mach-O
- **Cluster:** 3 bank servers + gateway as separate processes on ports 9000–9003, fresh state before every test

## Summary

- **pytest:** 47 passed, 0 failed, 0 skipped (total 47, 30.462 s)
- **Recorded test cases:** 20 / 20 passed
- **pytest exit code:** 0

## Test Case Results

| # | Test Case | Input/Scenario | Expected | Actual | Status |
|---|-----------|----------------|----------|--------|--------|
| TC1 | Deposit | A101=10000; DEPOSIT 2000 | A101=12000, SUCCESS | A101=12000, SUCCESS | ✅ |
| TC2 | Withdrawal | A101=10000; WITHDRAW 3000 | A101=7000, SUCCESS | A101=7000, SUCCESS | ✅ |
| TC3 | Insufficient Balance | A101=10000; WITHDRAW 15000 | FAILED: Insufficient balance, A101=10000 | FAILED: Insufficient balance, A101=10000 | ✅ |
| TC4 | Successful Distributed Transfer | A101=10000, A201=20000; TRANSFER 5000 (Server 1 → Server 2) | A101=5000, A201=25000, SUCCESS | A101=5000, A201=25000, SUCCESS, held=0/0 | ✅ |
| TC5a | Concurrent Transfers (same source) | A101=10000; 20 × TRANSFER ₹1000 to A201/A301 concurrently | 10 SUCCESS, 10 FAILED (Insufficient balance), A101=0, never negative | 10 SUCCESS, 10 FAILED, A101=0, credited ₹10000 | ✅ |
| TC5b | Concurrent Transfers (opposite directions) | A101=10000, A201=20000; 5 × A101→A201 ₹1000 and 5 × A201→A101 ₹2000 concurrently | all complete (no deadlock), A101=15000, A201=15000, total 30000 | 10 SUCCESS, A101=15000, A201=15000 | ✅ |
| TC6 | Concurrent Withdrawal | A101=10000; T1, T2 WITHDRAW ₹7000 concurrently | 1 SUCCESS, 1 FAILED, A101=3000 | 1 SUCCESS, 1 FAILED, A101=3000 | ✅ |
| TC6b | Concurrent Withdrawal (§5 demo) | A101=10000; T1, T2 WITHDRAW ₹8000 concurrently (assignment §5) | 1 SUCCESS, 1 FAILED, A101=2000, not negative | 1 SUCCESS, A101=2000 | ✅ |
| TC6×50 | Concurrent Withdrawal ×50 | TC6 race repeated 50 times | 50/50 rounds: exactly 1 SUCCESS, A101=3000 | 50/50 rounds correct | ✅ |
| TC7a | Server Failure: down before txn | Kill S2; TRANSFER A101→A201 5000 | FAILED: destination server unavailable, A101=10000, held=0 | FAILED: Destination server unavailable (Server 2), A101=10000, held=0 | ✅ |
| TC7b | Server Failure: crash during PREPARE | Arm crash on S2 at on_prepare_before_vote; TRANSFER A101→A201 5000; restart S2 | FAILED, A101=10000 (held 0), A201=20000, no in-doubt rows after recovery | FAILED: Destination server unavailable (Server 2), A101=10000 (held 0), A201=20000, in-doubt on S2: 0 | ✅ |
| TC7c | Server Failure: crash during COMMIT | Arm crash on S2 at on_commit_before_apply; TRANSFER A101→A201 5000; restart S2 | PENDING (not SUCCESS) while S2 is down; after recovery A101=5000, A201=25000, log SUCCESS | response PENDING; after recovery A101=5000, A201=25000, log SUCCESS | ✅ |
| TC7d | Server Failure: destination unavailable | Kill S3; TRANSFER A101→A301 1000 | FAILED: Destination server unavailable, A101=10000 | FAILED: Destination server unavailable (Server 3), A101=10000, held=0 | ✅ |
| TC8 | Invalid Account | TRANSFER A101 A999 5000 | FAILED: Destination account not found, A101 unchanged | FAILED: Destination account not found, A101=10000 | ✅ |
| Stress | Money conservation stress | 6 accounts; 500 random concurrent transfers, 32 threads | total stays ₹135000, no negative balance, all holds 0 | 463 SUCCESS / 37 FAILED; total ₹135000; min balance ₹10244; holds 0; in-doubt 0 | ✅ |
| Log | Transaction log completeness | DEPOSIT, WITHDRAW, failed WITHDRAW, 2PC/local/invalid TRANSFER | 6 records, each with txn_id, source, destination (transfers), amount, timestamp, status, failure_reason (if failed) | 6 records, all fields present | ✅ |
| 2PC-timeout | 2PC: vote timeout releases hold | S1 delays its vote 4500 ms (> read timeout); TRANSFER A101→A201 5000 | FAILED, A101 hold released, A101=10000 | FAILED: Source server (Server 1) did not respond in time, held=0 | ✅ |
| 2PC-coord-crash | 2PC: coordinator crash before decision | Gateway killed after S1 prepared (no decision); restart gateway | Transfer resolved as ABORT, A101 hold released, balances unchanged | log FAILED (Coordinator restarted before a decision was made), held=0 | ✅ |
| Idempotent commit | 2PC: duplicate COMMITs | 20 × (PREPARE credit ₹100 on A201; 8 concurrent COMMITs + 1 more) | all acknowledged, each credit applied once (A201=22000) | 180/180 acknowledged, A201=22000 | ✅ |
| Durability | Durability across restart | DEPOSIT A101 2500; TRANSFER A101→A201 1500; restart S1 | A101=11000, A201=21500 after restart | A101=11000, A201=21500 | ✅ |

## Transaction Log Excerpts

Global transaction log records captured during each test (the same records are mirrored to `logs/transactions.jsonl`).

### TC1 — Deposit

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-96d92c | DEPOSIT | A101 |  | 2000 | 2026-10-10T19:03:56.438324+05:30 | SUCCESS |  |  |

### TC2 — Withdrawal

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-e25140 | WITHDRAW | A101 |  | 3000 | 2026-10-10T19:03:56.450441+05:30 | SUCCESS |  |  |

### TC3 — Insufficient Balance

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-5892b4 | WITHDRAW | A101 |  | 15000 | 2026-10-10T19:03:56.460397+05:30 | FAILED | Insufficient balance |  |

### TC4 — Successful Distributed Transfer

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-b9cfb4 | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:04:25.498512+05:30 | SUCCESS |  | COMMIT |

### TC6 — Concurrent Withdrawal

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-921e5d | WITHDRAW | A101 |  | 7000 | 2026-10-10T19:03:56.593621+05:30 | FAILED | Insufficient balance |  |
| T-20261010-088e0f | WITHDRAW | A101 |  | 7000 | 2026-10-10T19:03:56.592329+05:30 | SUCCESS |  |  |

### TC7a — Server Failure: down before txn

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-a0bbcc | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:03:58.692980+05:30 | FAILED | Destination server unavailable (Server 2) | ABORT |

### TC7b — Server Failure: crash during PREPARE

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-737608 | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:03:58.917512+05:30 | FAILED | Destination server unavailable (Server 2) | ABORT |

### TC7c — Server Failure: crash during COMMIT

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-6392ff | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:03:59.421310+05:30 | PENDING |  | COMMIT |
| T-20261010-6392ff | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:03:59.421310+05:30 | SUCCESS |  | COMMIT |

### TC7d — Server Failure: destination unavailable

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-793d07 | TRANSFER | A101 | A301 | 1000 | 2026-10-10T19:04:04.462020+05:30 | FAILED | Destination server unavailable (Server 3) | ABORT |

### TC8 — Invalid Account

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-2876c9 | TRANSFER | A101 | A999 | 5000 | 2026-10-10T19:04:25.532646+05:30 | FAILED | Destination account not found |  |

### Log — Transaction log completeness

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-42783b | DEPOSIT | A101 |  | 1000 | 2026-10-10T19:04:25.453894+05:30 | SUCCESS |  |  |
| T-20261010-2fe861 | WITHDRAW | A101 |  | 500 | 2026-10-10T19:04:25.456405+05:30 | SUCCESS |  |  |
| T-20261010-ed5cda | WITHDRAW | A101 |  | 999999 | 2026-10-10T19:04:25.458195+05:30 | FAILED | Insufficient balance |  |
| T-20261010-cdd3f8 | TRANSFER | A101 | A201 | 2000 | 2026-10-10T19:04:25.459051+05:30 | SUCCESS |  | COMMIT |
| T-20261010-a3e865 | TRANSFER | A101 | A102 | 100 | 2026-10-10T19:04:25.466796+05:30 | SUCCESS |  |  |
| T-20261010-c44b44 | TRANSFER | A101 | A999 | 100 | 2026-10-10T19:04:25.467560+05:30 | FAILED | Destination account not found |  |

### 2PC-coord-crash — 2PC: coordinator crash before decision

| txn_id | type | source | destination | amount | timestamp | status | failure_reason | decision |
|---|---|---|---|---|---|---|---|---|
| T-20261010-9c1203 | TRANSFER | A101 | A201 | 5000 | 2026-10-10T19:04:19.980551+05:30 | FAILED | Coordinator restarted before a decision was made | ABORT |

## Full pytest Output

Also in pytest_output.txt and junit.xml.

```
============================= test session starts ==============================
platform darwin -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0 -- /Users/adyasinghal/Padhai/Semester V/CSD438 DS/bank-venv/bin/python
rootdir: /Users/adyasinghal/Padhai/Semester V/CSD438 DS/MidSem_Project/test-cases
configfile: pytest.ini
plugins: anyio-4.15.1
collecting ... collected 47 items

../test-cases/test_basic_ops.py::TestBasicOps::test_tc1_deposit PASSED   [  2%]
../test-cases/test_basic_ops.py::TestBasicOps::test_tc2_withdrawal PASSED [  4%]
../test-cases/test_basic_ops.py::TestBasicOps::test_tc3_insufficient_balance PASSED [  6%]
../test-cases/test_basic_ops.py::TestBasicOps::test_balance_check PASSED [  8%]
../test-cases/test_basic_ops.py::TestBasicOps::test_create_account PASSED [ 10%]
../test-cases/test_basic_ops.py::TestBasicOps::test_duplicate_create PASSED [ 12%]
../test-cases/test_basic_ops.py::TestBasicOps::test_list_accounts PASSED [ 14%]
../test-cases/test_concurrency.py::TestConcurrency::test_tc5_concurrent_transfers_drain PASSED [ 17%]
../test-cases/test_concurrency.py::TestConcurrency::test_tc5b_bidirectional_transfer_no_deadlock PASSED [ 19%]
../test-cases/test_concurrency.py::TestConcurrency::test_tc6_concurrent_withdraw PASSED [ 21%]
../test-cases/test_concurrency.py::TestConcurrency::test_tc6b_assignment_demo PASSED [ 23%]
../test-cases/test_concurrency.py::TestConcurrency::test_tc6_concurrent_withdraw_repeated PASSED [ 25%]
../test-cases/test_concurrency.py::TestConcurrency::test_money_conservation_stress PASSED [ 27%]
../test-cases/test_failures.py::TestFailures::test_tc7a_server_down_before_txn PASSED [ 29%]
../test-cases/test_failures.py::TestFailures::test_tc7b_crash_during_prepare PASSED [ 31%]
../test-cases/test_failures.py::TestFailures::test_tc7c_crash_during_commit PASSED [ 34%]
../test-cases/test_failures.py::TestFailures::test_tc7d_destination_unavailable PASSED [ 36%]
../test-cases/test_failures.py::TestFailures::test_single_op_server_down PASSED [ 38%]
../test-cases/test_failures.py::TestFailures::test_prepare_timeout_releases_hold PASSED [ 40%]
../test-cases/test_failures.py::TestFailures::test_abort_overtakes_prepare PASSED [ 42%]
../test-cases/test_failures.py::TestFailures::test_coordinator_crash_before_decision PASSED [ 44%]
../test-cases/test_failures.py::TestFailures::test_duplicate_commit_applied_once PASSED [ 46%]
../test-cases/test_failures.py::TestFailures::test_durability_across_restart PASSED [ 48%]
../test-cases/test_logging.py::TestLogging::test_log_record_completeness PASSED [ 51%]
../test-cases/test_logging.py::TestLogging::test_jsonl_mirror PASSED     [ 53%]
../test-cases/test_logging.py::TestLogging::test_history_ordered PASSED  [ 55%]
../test-cases/test_logging.py::TestLogging::test_failed_transaction_logged PASSED [ 57%]
../test-cases/test_transfer.py::TestTransfer::test_tc4_cross_server_transfer PASSED [ 59%]
../test-cases/test_transfer.py::TestTransfer::test_same_server_transfer PASSED [ 61%]
../test-cases/test_transfer.py::TestTransfer::test_transfer_insufficient_funds PASSED [ 63%]
../test-cases/test_transfer.py::TestTransfer::test_tc8_invalid_destination PASSED [ 65%]
../test-cases/test_transfer.py::TestTransfer::test_tc8_missing_account_on_real_server[A105] PASSED [ 68%]
../test-cases/test_transfer.py::TestTransfer::test_tc8_missing_account_on_real_server[A205] PASSED [ 70%]
../test-cases/test_transfer.py::TestTransfer::test_source_not_found[A999] PASSED [ 72%]
../test-cases/test_transfer.py::TestTransfer::test_source_not_found[A105] PASSED [ 74%]
../test-cases/test_transfer.py::TestTransfer::test_source_not_found[A205] PASSED [ 76%]
../test-cases/test_transfer.py::TestTransfer::test_self_transfer PASSED  [ 78%]
../test-cases/test_validation.py::TestValidation::test_zero_amount_deposit PASSED [ 80%]
../test-cases/test_validation.py::TestValidation::test_negative_amount_withdraw PASSED [ 82%]
../test-cases/test_validation.py::TestValidation::test_non_integer_amount PASSED [ 85%]
../test-cases/test_validation.py::TestValidation::test_negative_initial_balance PASSED [ 87%]
../test-cases/test_validation.py::TestValidation::test_empty_owner_name PASSED [ 89%]
../test-cases/test_validation.py::TestValidation::test_invalid_account_id_format PASSED [ 91%]
../test-cases/test_validation.py::TestValidation::test_create_unknown_server_prefix PASSED [ 93%]
../test-cases/test_validation.py::TestValidation::test_zero_transfer_amount PASSED [ 95%]
../test-cases/test_validation.py::TestValidation::test_self_transfer_rejected PASSED [ 97%]
../test-cases/test_validation.py::TestValidation::test_unknown_account_deposit PASSED [100%]

- generated xml file: /Users/adyasinghal/Padhai/Semester V/CSD438 DS/MidSem_Project/results/junit.xml -
============================= 47 passed in 30.46s ==============================

```
