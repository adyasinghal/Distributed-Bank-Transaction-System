"""TC5, TC6: concurrent transfers and withdrawals, money conservation."""

import random
import threading
from concurrent.futures import ThreadPoolExecutor

ALL_ACCOUNTS = ["A101", "A102", "A201", "A202", "A301", "A302"]


def run_together(fns: list) -> list:
    """Run the callables on separate threads released at the same instant by a barrier."""
    results = [None] * len(fns)
    barrier = threading.Barrier(len(fns), timeout=10)

    def worker(i):
        barrier.wait()
        results[i] = fns[i]()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "threads did not finish (deadlock?)"
    return results


class TestConcurrency:
    """Test cases for concurrent operations."""

    def test_tc5_concurrent_transfers_drain(self, cluster, obs):
        """TC5a: 20 concurrent cross-server transfers of ₹1000 from A101 (balance=10000)."""
        def make(i):
            dest = "A201" if i % 2 == 0 else "A301"
            return lambda: cluster.transfer("A101", dest, 1000)

        responses = run_together([make(i) for i in range(20)])
        statuses = [r.json()["status"] for r in responses]
        successes = statuses.count("SUCCESS")
        failures = statuses.count("FAILED")
        reasons = {r.json()["message"] for r in responses if r.json()["status"] == "FAILED"}

        bal = cluster.balance("A101")
        received = (cluster.balance("A201") - 20000) + (cluster.balance("A301") - 30000)

        assert successes == 10
        assert failures == 10
        assert reasons == {"Insufficient balance"}
        assert bal == 0
        assert received == 10000  # every successful debit was credited exactly once
        assert cluster.get_held("A101") == 0

        obs("TC5a", "A101=10000; 20 × TRANSFER ₹1000 to A201/A301 concurrently",
            "10 SUCCESS, 10 FAILED (Insufficient balance), A101=0, never negative",
            f"{successes} SUCCESS, {failures} FAILED, A101={bal}, credited ₹{received}",
            successes == 10 and bal == 0 and received == 10000)

    def test_tc5b_bidirectional_transfer_no_deadlock(self, cluster, obs):
        """TC5b: Concurrent opposite-direction transfers (A101⇄A201), several each way."""
        fns = []
        for _ in range(5):
            fns.append(lambda: cluster.transfer("A101", "A201", 1000))
            fns.append(lambda: cluster.transfer("A201", "A101", 2000))
        responses = run_together(fns)
        statuses = [r.json()["status"] for r in responses]
        assert statuses.count("SUCCESS") == 10

        a101, a201 = cluster.balance("A101"), cluster.balance("A201")
        assert a101 == 10000 - 5 * 1000 + 5 * 2000
        assert a201 == 20000 + 5 * 1000 - 5 * 2000
        assert a101 + a201 == 30000

        obs("TC5b", "A101=10000, A201=20000; 5 × A101→A201 ₹1000 and 5 × A201→A101 ₹2000 concurrently",
            "all complete (no deadlock), A101=15000, A201=15000, total 30000",
            f"{statuses.count('SUCCESS')} SUCCESS, A101={a101}, A201={a201}",
            a101 == 15000 and a201 == 15000)

    def test_tc6_concurrent_withdraw(self, cluster, obs):
        """TC6: Two concurrent withdrawals of ₹7000 from A101 (balance=10000)."""
        responses = run_together([lambda: cluster.withdraw("A101", 7000)] * 2)
        codes = [r.status_code for r in responses]
        success_count = codes.count(200)
        fail_count = codes.count(409)
        bal = cluster.balance("A101")

        assert success_count == 1
        assert fail_count == 1
        assert bal == 3000

        obs("TC6", "A101=10000; T1, T2 WITHDRAW ₹7000 concurrently",
            "1 SUCCESS, 1 FAILED, A101=3000",
            f"{success_count} SUCCESS, {fail_count} FAILED, A101={bal}",
            success_count == 1 and bal == 3000,
            log=[cluster.txn(r.json()["txn_id"]) for r in responses])

    def test_tc6b_assignment_demo(self, cluster, obs):
        """TC6b (§5 demo): A101=10000, two concurrent WITHDRAW ₹8000."""
        responses = run_together([lambda: cluster.withdraw("A101", 8000)] * 2)
        success_count = [r.status_code for r in responses].count(200)
        bal = cluster.balance("A101")

        assert success_count == 1
        assert bal == 2000

        obs("TC6b", "A101=10000; T1, T2 WITHDRAW ₹8000 concurrently (assignment §5)",
            "1 SUCCESS, 1 FAILED, A101=2000, not negative",
            f"{success_count} SUCCESS, A101={bal}",
            success_count == 1 and bal == 2000)

    def test_tc6_concurrent_withdraw_repeated(self, cluster, obs):
        """Repeat the TC6 race 50 times for confidence."""
        rounds = 50
        good = 0
        for _ in range(rounds):
            cluster.reset(seed=True)
            responses = run_together([lambda: cluster.withdraw("A101", 7000)] * 2)
            ok = ([r.status_code for r in responses].count(200) == 1
                  and cluster.balance("A101") == 3000)
            good += ok
        assert good == rounds

        obs("TC6×50", f"TC6 race repeated {rounds} times",
            f"{rounds}/{rounds} rounds: exactly 1 SUCCESS, A101=3000",
            f"{good}/{rounds} rounds correct", good == rounds)

    def test_money_conservation_stress(self, cluster, obs):
        """Stress test: 6 accounts, 500 random concurrent transfers on 32 threads."""
        rng = random.Random(42)
        initial_total = sum(cluster.balance(a) for a in ALL_ACCOUNTS)

        ops = []
        for _ in range(500):
            src = rng.choice(ALL_ACCOUNTS)
            dst = rng.choice([a for a in ALL_ACCOUNTS if a != src])
            ops.append((src, dst, rng.randint(1, 5000)))

        with ThreadPoolExecutor(max_workers=32) as pool:
            statuses = list(pool.map(lambda op: cluster.transfer(*op).json()["status"], ops))

        final = {a: cluster.balance(a) for a in ALL_ACCOUNTS}
        final_total = sum(final.values())
        held = {a: cluster.get_held(a) for a in ALL_ACCOUNTS}
        in_doubt = sum(len(cluster.prepared(sid)) for sid in (1, 2, 3))

        assert "PENDING" not in statuses
        assert final_total == initial_total
        assert all(b >= 0 for b in final.values())
        assert all(h == 0 for h in held.values())
        assert in_doubt == 0

        obs("Stress", "6 accounts; 500 random concurrent transfers, 32 threads",
            f"total stays ₹{initial_total}, no negative balance, all holds 0",
            f"{statuses.count('SUCCESS')} SUCCESS / {statuses.count('FAILED')} FAILED; "
            f"total ₹{final_total}; min balance ₹{min(final.values())}; "
            f"holds {sum(held.values())}; in-doubt {in_doubt}",
            final_total == initial_total and all(h == 0 for h in held.values()))
