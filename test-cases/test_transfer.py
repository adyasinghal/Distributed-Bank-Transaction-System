"""TC4, TC8: cross-server, same-server and invalid-account transfers."""

import pytest

from config import server_for


class TestTransfer:
    """Test cases for fund transfers."""

    def test_tc4_cross_server_transfer(self, cluster, obs):
        """TC4: Cross-server transfer (A101→A201) via 2PC."""
        assert server_for("A101") != server_for("A201")  # really distributed

        resp = cluster.transfer("A101", "A201", 5000)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"

        src_bal = cluster.balance("A101")
        dst_bal = cluster.balance("A201")
        assert src_bal == 5000
        assert dst_bal == 25000

        # Verify holds are released and nothing is left in doubt
        src_held = cluster.get_held("A101")
        dst_held = cluster.get_held("A201")
        assert src_held == 0
        assert dst_held == 0
        assert cluster.prepared(1) == [] and cluster.prepared(2) == []

        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "SUCCESS" and rec["decision"] == "COMMIT"
        obs("TC4", "A101=10000, A201=20000; TRANSFER 5000 (Server 1 → Server 2)",
            "A101=5000, A201=25000, SUCCESS",
            f"A101={src_bal}, A201={dst_bal}, {data['status']}, held={src_held}/{dst_held}",
            src_bal == 5000 and dst_bal == 25000, log=[rec])

    def test_same_server_transfer(self, cluster):
        """Same-server transfer (A101→A102) uses local path."""
        resp = cluster.transfer("A101", "A102", 3000)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"

        assert cluster.balance("A101") == 7000
        assert cluster.balance("A102") == 18000
        assert cluster.txn(data["txn_id"])["status"] == "SUCCESS"

    def test_transfer_insufficient_funds(self, cluster):
        """Cross-server transfer with insufficient funds fails and leaves no hold."""
        resp = cluster.transfer("A101", "A201", 50000)
        assert resp.status_code == 409
        assert resp.json()["message"] == "Insufficient balance"

        # Source unchanged
        assert cluster.balance("A101") == 10000
        assert cluster.balance("A201") == 20000
        assert cluster.get_held("A101") == 0

    def test_tc8_invalid_destination(self, cluster, obs):
        """TC8: Transfer to non-existent account (unknown server prefix)."""
        resp = cluster.transfer("A101", "A999", 5000)
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "FAILED"
        assert data["message"] == "Destination account not found"

        # Source unchanged
        bal = cluster.balance("A101")
        assert bal == 10000

        # The rejected request is still a logged transaction
        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "FAILED"
        assert rec["failure_reason"] == "Destination account not found"
        assert rec["source"] == "A101" and rec["destination"] == "A999"

        obs("TC8", "TRANSFER A101 A999 5000",
            "FAILED: Destination account not found, A101 unchanged",
            f"FAILED: {data['message']}, A101={bal}",
            bal == 10000 and data["message"] == "Destination account not found", log=[rec])

    @pytest.mark.parametrize("dest", ["A105", "A205"])
    def test_tc8_missing_account_on_real_server(self, cluster, dest):
        """TC8 variant: valid server prefix, account doesn't exist (same-server and 2PC paths)."""
        resp = cluster.transfer("A101", dest, 5000)
        assert resp.status_code == 404
        assert resp.json()["message"] == "Destination account not found"
        assert cluster.balance("A101") == 10000
        assert cluster.get_held("A101") == 0

    @pytest.mark.parametrize("source", ["A999", "A105", "A205"])
    def test_source_not_found(self, cluster, source):
        """Transfer from a non-existent source gives the exact source message."""
        resp = cluster.transfer(source, "A101", 1000)
        assert resp.status_code == 404
        assert resp.json()["message"] == "Source account not found"
        assert cluster.balance("A101") == 10000

    def test_self_transfer(self, cluster):
        """Transfer to self is rejected and logged."""
        resp = cluster.transfer("A101", "A101", 1000)
        assert resp.status_code == 400
        rec = cluster.txn(resp.json()["txn_id"])
        assert rec["status"] == "FAILED"
