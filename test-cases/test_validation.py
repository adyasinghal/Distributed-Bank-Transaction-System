"""Input validation."""


class TestValidation:
    """Test input validation and edge cases."""

    def _assert_rejected(self, resp, field: str):
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "FAILED"
        assert field in data["message"]

    def test_zero_amount_deposit(self, cluster):
        """Zero amount deposit is rejected."""
        self._assert_rejected(cluster.deposit("A101", 0), "amount")

    def test_negative_amount_withdraw(self, cluster):
        """Negative amount withdrawal is rejected."""
        self._assert_rejected(cluster.withdraw("A101", -100), "amount")

    def test_non_integer_amount(self, cluster):
        """Fractional amount is rejected (money is whole rupees)."""
        resp = cluster.http.post(f"{cluster.gateway_url}/accounts/A101/deposit", json={"amount": 10.5})
        self._assert_rejected(resp, "amount")
        assert cluster.balance("A101") == 10000

    def test_negative_initial_balance(self, cluster):
        """Account creation with negative balance is rejected."""
        self._assert_rejected(cluster.create("A103", "Test", -1000), "initial_balance")

    def test_empty_owner_name(self, cluster):
        """Account creation with empty owner is rejected."""
        self._assert_rejected(cluster.create("A103", "", 1000), "owner")

    def test_invalid_account_id_format(self, cluster):
        """Invalid account ID format is rejected."""
        self._assert_rejected(cluster.create("BADID", "Test", 1000), "account_id")

    def test_create_unknown_server_prefix(self, cluster):
        """A well-formed ID whose prefix maps to no server is rejected."""
        resp = cluster.create("A901", "Test", 1000)
        assert resp.status_code == 400
        assert "Invalid account ID" in resp.json()["message"]

    def test_zero_transfer_amount(self, cluster):
        """Zero transfer amount is rejected."""
        self._assert_rejected(cluster.transfer("A101", "A201", 0), "amount")

    def test_self_transfer_rejected(self, cluster):
        """Transfer to self is rejected."""
        resp = cluster.transfer("A101", "A101", 1000)
        assert resp.status_code == 400
        assert resp.json()["message"] == "Cannot transfer to the same account"

    def test_unknown_account_deposit(self, cluster):
        """Deposit to a missing account fails with 404 and is logged."""
        resp = cluster.deposit("A105", 100)
        assert resp.status_code == 404
        assert resp.json()["message"] == "Account not found"
        assert cluster.txn(resp.json()["txn_id"])["status"] == "FAILED"
