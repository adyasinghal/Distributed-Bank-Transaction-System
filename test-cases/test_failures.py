"""TC7: server failures, plus 2PC timeout, coordinator crash, duplicate commits, durability."""

import threading
import time

import httpx
import config


class TestFailures:
    """Test cases for failure scenarios and recovery."""

    def test_tc7a_server_down_before_txn(self, cluster, obs):
        """TC7a: Server 2 is down before the transfer starts."""
        cluster.kill(2)

        resp = cluster.transfer("A101", "A201", 5000)
        data = resp.json()
        assert resp.status_code == 503
        assert data["status"] == "FAILED"
        assert "unavailable" in data["message"].lower() and "Server 2" in data["message"]

        bal = cluster.balance("A101")
        held = cluster.get_held("A101")
        rec = cluster.txn(data["txn_id"])
        assert bal == 10000
        assert held == 0
        assert rec["status"] == "FAILED" and rec["decision"] == "ABORT"

        obs("TC7a", "Kill S2; TRANSFER A101→A201 5000",
            "FAILED: destination server unavailable, A101=10000, held=0",
            f"FAILED: {data['message']}, A101={bal}, held={held}",
            bal == 10000 and held == 0, log=[rec])

    def test_tc7b_crash_during_prepare(self, cluster, obs):
        """TC7b: Server 2 crashes during PREPARE (after persisting PREPARED, before voting)."""
        cluster.arm_fault(2, "on_prepare_before_vote", "crash")

        resp = cluster.transfer("A101", "A201", 5000)
        data = resp.json()
        assert data["status"] == "FAILED"
        assert cluster.wait_dead(2)

        bal_a101 = cluster.balance("A101")
        held_a101 = cluster.get_held("A101")
        assert bal_a101 == 10000
        assert held_a101 == 0

        # Restart S2 — recovery should resolve the PREPARED row as ABORT
        cluster.start(2)
        assert cluster.wait_until(lambda: cluster.prepared(2) == [], timeout=10)

        bal_a201 = cluster.balance("A201")
        assert bal_a201 == 20000
        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "FAILED" and rec["decision"] == "ABORT"

        obs("TC7b", "Arm crash on S2 at on_prepare_before_vote; TRANSFER A101→A201 5000; restart S2",
            "FAILED, A101=10000 (held 0), A201=20000, no in-doubt rows after recovery",
            f"FAILED: {data['message']}, A101={bal_a101} (held {held_a101}), A201={bal_a201}, "
            f"in-doubt on S2: {len(cluster.prepared(2))}",
            bal_a101 == 10000 and bal_a201 == 20000, log=[rec])

    def test_tc7c_crash_during_commit(self, cluster, obs):
        """TC7c: Server 2 crashes during COMMIT (after the COMMIT decision)."""
        cluster.arm_fault(2, "on_commit_before_apply", "crash")

        resp = cluster.transfer("A101", "A201", 5000)
        data = resp.json()
        assert resp.status_code == 202
        assert data["status"] == "PENDING"  # critical: no false SUCCESS
        assert cluster.wait_dead(2)

        # Source is committed (debit applied); destination credit is outstanding
        bal_a101 = cluster.balance("A101")
        assert bal_a101 == 5000
        rec_during = cluster.txn(data["txn_id"])
        assert rec_during["status"] == "PENDING" and rec_during["decision"] == "COMMIT"

        # Restart S2 — server recovery and gateway recovery both try to commit;
        # the credit must still be applied exactly once
        cluster.start(2)
        assert cluster.wait_until(lambda: cluster.txn(data["txn_id"])["status"] == "SUCCESS", timeout=15)
        time.sleep(2 * config.RECOVERY_RETRY_INTERVAL)  # let any duplicate retry land

        bal_a201 = cluster.balance("A201")
        rec_after = cluster.txn(data["txn_id"])
        assert bal_a201 == 25000
        assert cluster.get_held("A101") == 0
        assert cluster.prepared(2) == []

        obs("TC7c", "Arm crash on S2 at on_commit_before_apply; TRANSFER A101→A201 5000; restart S2",
            "PENDING (not SUCCESS) while S2 is down; after recovery A101=5000, A201=25000, log SUCCESS",
            f"response {data['status']}; after recovery A101={bal_a101}, A201={bal_a201}, "
            f"log {rec_after['status']}",
            data["status"] == "PENDING" and bal_a101 == 5000 and bal_a201 == 25000
            and rec_after["status"] == "SUCCESS",
            log=[rec_during, rec_after])

    def test_tc7d_destination_unavailable(self, cluster, obs):
        """TC7d: Destination server (S3) is down."""
        cluster.kill(3)

        resp = cluster.transfer("A101", "A301", 1000)
        data = resp.json()
        assert data["status"] == "FAILED"
        assert data["message"] == "Destination server unavailable (Server 3)"

        bal = cluster.balance("A101")
        held = cluster.get_held("A101")
        assert bal == 10000
        assert held == 0

        obs("TC7d", "Kill S3; TRANSFER A101→A301 1000",
            "FAILED: Destination server unavailable, A101=10000",
            f"FAILED: {data['message']}, A101={bal}, held={held}",
            bal == 10000 and held == 0, log=[cluster.txn(data["txn_id"])])

    def test_single_op_server_down(self, cluster):
        """BALANCE/DEPOSIT on a dead server return 503 and the deposit is logged as FAILED."""
        cluster.kill(2)

        resp = cluster.http.get(f"{cluster.gateway_url}/accounts/A201/balance")
        assert resp.status_code == 503
        assert resp.json()["message"] == "Server 2 unavailable"

        resp = cluster.deposit("A201", 100)
        assert resp.status_code == 503
        assert cluster.txn(resp.json()["txn_id"])["status"] == "FAILED"

    def test_prepare_timeout_releases_hold(self, cluster, obs):
        """The source places the hold, then is too slow to vote (read timeout)."""
        delay_ms = int(config.READ_TIMEOUT * 1000) + 1500
        cluster.arm_fault(1, "on_prepare_before_vote", "delay", delay_ms=delay_ms)

        resp = cluster.transfer("A101", "A201", 5000)
        assert resp.status_code == 504
        assert resp.json()["message"] == "Source server (Server 1) did not respond in time"
        held_now = cluster.get_held("A101")
        time.sleep(delay_ms / 1000)  # let the slow PREPARE finish
        held_later = cluster.get_held("A101")

        assert held_now == 0 and held_later == 0
        assert cluster.balance("A101") == 10000
        assert cluster.prepared(1) == []

        obs("2PC-timeout", f"S1 delays its vote {delay_ms} ms (> read timeout); TRANSFER A101→A201 5000",
            "FAILED, A101 hold released, A101=10000",
            f"{resp.json()['status']}: {resp.json()['message']}, held={held_later}",
            held_later == 0)

    def test_abort_overtakes_prepare(self, cluster):
        """ABORT reaches the server before a delayed PREPARE; the PREPARE must not place a hold."""
        delay_ms = int(config.READ_TIMEOUT * 1000) + 1500
        cluster.arm_fault(1, "on_request", "delay", delay_ms=delay_ms)

        resp = cluster.transfer("A101", "A201", 5000)
        assert resp.json()["status"] == "FAILED"
        time.sleep(delay_ms / 1000)  # let the late PREPARE run

        assert cluster.get_held("A101") == 0
        assert cluster.balance("A101") == 10000
        assert cluster.prepared(1) == []

    def test_coordinator_crash_before_decision(self, cluster, obs):
        """Gateway crashes before deciding; after restart the transfer is aborted and the hold released."""
        cluster.arm_fault(2, "on_prepare_before_vote", "delay", delay_ms=2500)
        t = threading.Thread(target=lambda: _swallow(lambda: cluster.transfer("A101", "A201", 5000)))
        t.start()
        assert cluster.wait_until(lambda: cluster.get_held("A101") == 5000, timeout=5)
        cluster.kill_gateway()
        t.join()

        cluster.start_gateway()
        released = cluster.wait_until(
            lambda: cluster.get_held("A101") == 0 and cluster.prepared(1) == [] and cluster.prepared(2) == [],
            timeout=20)
        log = cluster.txn_log()

        assert released
        assert cluster.balance("A101") == 10000 and cluster.balance("A201") == 20000
        assert log[0]["status"] == "FAILED" and log[0]["decision"] == "ABORT"

        obs("2PC-coord-crash", "Gateway killed after S1 prepared (no decision); restart gateway",
            "Transfer resolved as ABORT, A101 hold released, balances unchanged",
            f"log {log[0]['status']} ({log[0]['failure_reason']}), held={cluster.get_held('A101')}",
            released, log=[log[0]])

    def test_duplicate_commit_applied_once(self, cluster, obs):
        """8 duplicate COMMITs at the same instant apply the credit once (20 rounds)."""
        s2 = cluster.server_urls[2]
        rounds, dup = 20, 8
        codes = []

        for i in range(rounds):
            txn_id = f"T-test-dupcommit-{i}"
            r = cluster.http.post(f"{s2}/2pc/prepare", json={
                "txn_id": txn_id, "account_id": "A201", "op": "credit", "amount": 100})
            assert r.json()["vote"] == "VOTE_COMMIT"

            barrier = threading.Barrier(dup)

            def commit():
                with httpx.Client(timeout=10) as h:
                    barrier.wait()
                    codes.append(h.post(f"{s2}/2pc/commit", json={"txn_id": txn_id}).status_code)

            threads = [threading.Thread(target=commit) for _ in range(dup)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            codes.append(cluster.http.post(f"{s2}/2pc/commit", json={"txn_id": txn_id}).status_code)

        bal = cluster.balance("A201")
        expected = 20000 + rounds * 100
        assert codes == [200] * (rounds * (dup + 1))
        assert bal == expected

        obs("Idempotent commit",
            f"{rounds} × (PREPARE credit ₹100 on A201; {dup} concurrent COMMITs + 1 more)",
            f"all acknowledged, each credit applied once (A201={expected})",
            f"{codes.count(200)}/{len(codes)} acknowledged, A201={bal}", bal == expected)

    def test_durability_across_restart(self, cluster, obs):
        """Balances survive a normal server restart (SQLite persistence)."""
        cluster.deposit("A101", 2500)
        cluster.transfer("A101", "A201", 1500)
        cluster.stop(1)
        cluster.start(1)

        a101, a201 = cluster.balance("A101"), cluster.balance("A201")
        assert a101 == 11000 and a201 == 21500

        obs("Durability", "DEPOSIT A101 2500; TRANSFER A101→A201 1500; restart S1",
            "A101=11000, A201=21500 after restart",
            f"A101={a101}, A201={a201}", a101 == 11000 and a201 == 21500)


def _swallow(fn):
    """Run fn, ignoring transport errors (the gateway is killed mid-request)."""
    try:
        fn()
    except httpx.TransportError:
        pass
