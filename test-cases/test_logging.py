"""Transaction log and history."""

import json


class TestLogging:
    """Test transaction log completeness and history."""

    def test_log_record_completeness(self, cluster, obs):
        """Every record has the 7 fields required by the assignment."""
        cluster.deposit("A101", 1000)
        cluster.withdraw("A101", 500)
        cluster.withdraw("A101", 999999)          # FAILED: insufficient
        cluster.transfer("A101", "A201", 2000)    # 2PC
        cluster.transfer("A101", "A102", 100)     # local
        cluster.transfer("A101", "A999", 100)     # FAILED: destination not found

        txns = cluster.txn_log()
        assert len(txns) == 6

        for t in txns:
            assert t["txn_id"].startswith("T-"), t
            assert t["type"], t
            assert t["source"], t
            assert t["amount"] > 0, t
            assert t["timestamp"], t
            assert t["status"] in ("SUCCESS", "FAILED"), t
            if t["type"] == "TRANSFER":
                assert t["destination"], t
            if t["status"] == "FAILED":
                assert t["failure_reason"], t

        obs("Log", "DEPOSIT, WITHDRAW, failed WITHDRAW, 2PC/local/invalid TRANSFER",
            "6 records, each with txn_id, source, destination (transfers), amount, timestamp, status, failure_reason (if failed)",
            f"{len(txns)} records, all fields present", True,
            log=sorted(txns, key=lambda t: t["timestamp"]))

    def test_jsonl_mirror(self, cluster):
        """Each record is also appended to the human-readable JSONL log."""
        resp = cluster.deposit("A101", 1000)
        txn_id = resp.json()["txn_id"]
        jsonl = cluster.data_dir / "logs" / "transactions.jsonl"
        lines = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
        assert any(rec["txn_id"] == txn_id and rec["status"] == "SUCCESS" for rec in lines)

    def test_history_ordered(self, cluster):
        """HISTORY returns transactions in chronological order."""
        cluster.deposit("A101", 100)
        cluster.deposit("A101", 200)
        cluster.transfer("A101", "A201", 300)
        cluster.deposit("A101", 400)

        resp = cluster.http.get(f"{cluster.gateway_url}/accounts/A101/transactions")
        assert resp.status_code == 200
        txns = resp.json()["data"]["transactions"]
        moves = [(t["type"], t["amount"]) for t in txns if t["type"] != "CREATE"]
        assert moves == [("DEPOSIT", 100), ("DEPOSIT", 200), ("2PC_DEBIT", 300), ("DEPOSIT", 400)]

    def test_failed_transaction_logged(self, cluster):
        """Failed transactions are also logged with failure_reason."""
        resp = cluster.withdraw("A101", 999999)  # should fail
        failed = cluster.http.get(f"{cluster.gateway_url}/transactions",
                                  params={"status": "FAILED", "limit": 10}).json()["data"]["transactions"]
        assert [t["txn_id"] for t in failed] == [resp.json()["txn_id"]]
        assert failed[0]["failure_reason"] == "Insufficient balance"
