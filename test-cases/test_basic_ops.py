"""TC1-TC3: deposit, withdrawal, insufficient balance."""


class TestBasicOps:
    """Test cases for single-account operations."""

    def test_tc1_deposit(self, cluster, obs):
        """TC1: Deposit increases balance correctly."""
        # A101 starts with 10000 (seeded)
        resp = cluster.deposit("A101", 2000)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"

        bal = cluster.balance("A101")
        assert bal == 12000
        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "SUCCESS" and rec["type"] == "DEPOSIT" and rec["amount"] == 2000
        obs("TC1", "A101=10000; DEPOSIT 2000", "A101=12000, SUCCESS",
            f"A101={bal}, {data['status']}", bal == 12000, log=[rec])

    def test_tc2_withdrawal(self, cluster, obs):
        """TC2: Withdrawal decreases balance correctly."""
        resp = cluster.withdraw("A101", 3000)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"

        bal = cluster.balance("A101")
        assert bal == 7000
        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "SUCCESS" and rec["type"] == "WITHDRAW" and rec["amount"] == 3000
        obs("TC2", "A101=10000; WITHDRAW 3000", "A101=7000, SUCCESS",
            f"A101={bal}, {data['status']}", bal == 7000, log=[rec])

    def test_tc3_insufficient_balance(self, cluster, obs):
        """TC3: Withdrawal with insufficient balance fails."""
        resp = cluster.withdraw("A101", 15000)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "FAILED"
        assert data["message"] == "Insufficient balance"

        bal = cluster.balance("A101")
        assert bal == 10000  # unchanged
        rec = cluster.txn(data["txn_id"])
        assert rec["status"] == "FAILED"
        assert rec["failure_reason"] == "Insufficient balance"
        obs("TC3", "A101=10000; WITHDRAW 15000", "FAILED: Insufficient balance, A101=10000",
            f"FAILED: {data['message']}, A101={bal}", bal == 10000, log=[rec])

    def test_balance_check(self, cluster):
        """BALANCE returns correct value."""
        bal = cluster.balance("A101")
        assert bal == 10000

    def test_create_account(self, cluster):
        """CREATE works and sets initial balance."""
        resp = cluster.create("A103", "TestUser", 5000)
        assert resp.status_code == 200
        bal = cluster.balance("A103")
        assert bal == 5000
        rec = cluster.txn(resp.json()["txn_id"])
        assert rec["type"] == "CREATE" and rec["status"] == "SUCCESS"

    def test_duplicate_create(self, cluster):
        """Duplicate CREATE returns 409 and is logged as FAILED."""
        resp = cluster.create("A101", "Duplicate", 9999)
        assert resp.status_code == 409
        # Original balance unchanged
        bal = cluster.balance("A101")
        assert bal == 10000
        rec = cluster.txn(resp.json()["txn_id"])
        assert rec["status"] == "FAILED" and rec["failure_reason"] == "Account already exists"

    def test_list_accounts(self, cluster):
        """ACCOUNTS lists the seed accounts of all three servers."""
        resp = cluster.http.get(f"{cluster.gateway_url}/accounts")
        accounts = {a["account_id"]: (a["owner"], a["server"], a["balance"])
                    for a in resp.json()["data"]["accounts"]}
        assert accounts == {
            "A101": ("Ram", 1, 10000), "A102": ("Sita", 1, 15000),
            "A201": ("Mohan", 2, 20000), "A202": ("Priya", 2, 25000),
            "A301": ("Arjun", 3, 30000), "A302": ("Lakshmi", 3, 35000),
        }
        assert sum(b for _, _, b in accounts.values()) == 135000
