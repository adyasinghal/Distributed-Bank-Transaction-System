"""Command-line client.

    python client/client.py                    # interactive
    python client/client.py --script FILE      # run commands from a file

Type HELP for the list of commands.
"""

import argparse
import sys
import threading
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_GATEWAY = "http://127.0.0.1:8000"


class BankClient:
    """CLI client that communicates with the gateway via HTTP."""

    def __init__(self, gateway_url: str):
        self.gateway = gateway_url.rstrip("/")
        self.client = httpx.Client(timeout=10.0)

    def run_command(self, line: str) -> str:
        """Parse and execute a single command line. Returns the output string."""
        parts = line.strip().split()
        if not parts:
            return ""

        cmd = parts[0].upper()

        try:
            if cmd == "CREATE":
                return self._create(parts)
            elif cmd == "DEPOSIT":
                return self._deposit(parts)
            elif cmd == "WITHDRAW":
                return self._withdraw(parts)
            elif cmd == "BALANCE":
                return self._balance(parts)
            elif cmd == "TRANSFER":
                return self._transfer(parts)
            elif cmd == "HISTORY":
                return self._history(parts)
            elif cmd == "LOG":
                return self._log(parts)
            elif cmd == "STATUS":
                return self._status(parts)
            elif cmd == "ACCOUNTS":
                return self._accounts()
            elif cmd == "HEALTH":
                return self._health()
            elif cmd == "RESET":
                return self._reset()
            elif cmd == "FAULT":
                return self._fault(parts)
            elif cmd == "PARALLEL":
                return self._parallel(line)
            elif cmd == "HELP":
                return self._help()
            elif cmd in ("EXIT", "QUIT"):
                return "__EXIT__"
            else:
                return f"Unknown command: {cmd}. Type HELP for usage."
        except httpx.ConnectError:
            return "FAILED: Gateway unreachable"
        except httpx.TimeoutException:
            return "FAILED: Request timed out"
        except Exception as e:
            return f"FAILED: {e}"

    def _create(self, parts: list[str]) -> str:
        if len(parts) < 4:
            return "Usage: CREATE <account_id> <owner> <initial_balance>"
        account_id, owner = parts[1], parts[2]
        try:
            balance = int(parts[3])
        except ValueError:
            return "FAILED: Balance must be an integer"
        resp = self.client.post(f"{self.gateway}/accounts", json={
            "account_id": account_id, "owner": owner, "initial_balance": balance,
        })
        return self._format(resp)

    def _deposit(self, parts: list[str]) -> str:
        if len(parts) < 3:
            return "Usage: DEPOSIT <account_id> <amount>"
        account_id = parts[1]
        try:
            amount = int(parts[2])
        except ValueError:
            return "FAILED: Amount must be an integer"
        resp = self.client.post(f"{self.gateway}/accounts/{account_id}/deposit", json={"amount": amount})
        return self._format(resp)

    def _withdraw(self, parts: list[str]) -> str:
        if len(parts) < 3:
            return "Usage: WITHDRAW <account_id> <amount>"
        account_id = parts[1]
        try:
            amount = int(parts[2])
        except ValueError:
            return "FAILED: Amount must be an integer"
        resp = self.client.post(f"{self.gateway}/accounts/{account_id}/withdraw", json={"amount": amount})
        return self._format(resp)

    def _balance(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: BALANCE <account_id>"
        account_id = parts[1]
        resp = self.client.get(f"{self.gateway}/accounts/{account_id}/balance")
        return self._format(resp)

    def _transfer(self, parts: list[str]) -> str:
        if len(parts) < 4:
            return "Usage: TRANSFER <source> <destination> <amount>"
        source, destination = parts[1], parts[2]
        try:
            amount = int(parts[3])
        except ValueError:
            return "FAILED: Amount must be an integer"
        resp = self.client.post(f"{self.gateway}/transfers", json={
            "source": source, "destination": destination, "amount": amount,
        })
        return self._format(resp)

    def _history(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: HISTORY <account_id>"
        account_id = parts[1]
        resp = self.client.get(f"{self.gateway}/accounts/{account_id}/transactions")
        if resp.status_code != 200:
            return self._format(resp)
        data = resp.json().get("data", {})
        txns = data.get("transactions", [])
        if not txns:
            return f"No transactions found for {account_id}"
        lines = [f"Transaction history for {account_id}:"]
        lines.append(f"{'TXN ID':<25} {'Type':<15} {'Amount':>8} {'Balance':>8} {'Status':<8} {'Time'}")
        lines.append("-" * 100)
        for t in txns:
            lines.append(
                f"{t.get('txn_id', ''):<25} {t.get('type', ''):<15} "
                f"₹{t.get('amount', 0):>7} ₹{t.get('balance_after', '?'):>7} "
                f"{t.get('status', ''):<8} {t.get('timestamp', '')}"
            )
        return "\n".join(lines)

    def _log(self, parts: list[str]) -> str:
        limit = 20
        if len(parts) > 1:
            try:
                limit = int(parts[1])
            except ValueError:
                pass
        resp = self.client.get(f"{self.gateway}/transactions", params={"limit": limit})
        if resp.status_code != 200:
            return self._format(resp)
        txns = resp.json().get("data", {}).get("transactions", [])
        if not txns:
            return "No transactions in the log."
        lines = ["Global Transaction Log:"]
        lines.append(f"{'TXN ID':<25} {'Type':<10} {'Source':<8} {'Dest':<8} {'Amount':>8} {'Status':<8} {'Reason'}")
        lines.append("-" * 100)
        for t in txns:
            lines.append(
                f"{t.get('txn_id', ''):<25} {t.get('type', ''):<10} "
                f"{t.get('source', ''):<8} {t.get('destination', ''):<8} "
                f"₹{t.get('amount', 0):>7} {t.get('status', ''):<8} "
                f"{t.get('failure_reason', '')}"
            )
        return "\n".join(lines)

    def _status(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: STATUS <txn_id>"
        txn_id = parts[1]
        resp = self.client.get(f"{self.gateway}/transactions/{txn_id}")
        if resp.status_code != 200:
            return self._format(resp)
        t = resp.json().get("data", {})
        out = (f"{t.get('status')}: {t.get('type')} {t.get('source', '')} "
               f"{t.get('destination') or ''} ₹{t.get('amount')}  [txn: {txn_id}]")
        if t.get("failure_reason"):
            out += f"  Reason: {t['failure_reason']}"
        if t.get("decision"):
            out += f"  2PC decision: {t['decision']}"
        return out

    def _accounts(self) -> str:
        resp = self.client.get(f"{self.gateway}/accounts")
        if resp.status_code != 200:
            return self._format(resp)
        data = resp.json()["data"]
        lines = [f"{'Account':<9} {'Owner':<10} {'Server':<7} {'Balance':>10} {'Held':>8}", "-" * 48]
        for a in data["accounts"]:
            lines.append(f"{a['account_id']:<9} {a['owner']:<10} {a['server']:<7} "
                         f"₹{a['balance']:>9} ₹{a['held']:>7}")
        lines.append("-" * 48)
        lines.append(f"{'Total':<28}₹{sum(a['balance'] for a in data['accounts']):>9}")
        for sid in data["unreachable"]:
            lines.append(f"Server {sid} unreachable: its accounts are not shown")
        return "\n".join(lines)

    def _health(self) -> str:
        resp = self.client.get(f"{self.gateway}/health")
        if resp.status_code != 200:
            return "FAILED: Could not check health"
        data = resp.json()
        lines = ["System Health:"]
        lines.append(f"  Gateway: {data.get('status', 'unknown')}")
        for srv, st in data.get("servers", {}).items():
            lines.append(f"  {srv}: {st}")
        return "\n".join(lines)

    def _reset(self) -> str:
        resp = self.client.post(f"{self.gateway}/admin/reset", json={"seed": True})
        if resp.status_code != 200:
            return f"FAILED: HTTP {resp.status_code}"
        errors = resp.json().get("errors")
        if errors:
            return f"FAILED: reset incomplete: {'; '.join(errors)}"
        return "SUCCESS: All servers reset to the seed accounts; transaction log cleared"

    def _fault(self, parts: list[str]) -> str:
        if len(parts) < 4:
            return "Usage: FAULT <server_id> <point> <action> [delay_ms] [always]"
        try:
            server_id = int(parts[1])
        except ValueError:
            return "FAILED: Server ID must be an integer"
        point, action = parts[2], parts[3]
        rest = parts[4:]
        always = "always" in (p.lower() for p in rest)
        numbers = [p for p in rest if p.isdigit()]
        delay_ms = int(numbers[0]) if numbers else 0

        import config as cfg
        url = cfg.SERVERS.get(server_id)
        if not url:
            return f"FAILED: Unknown server {server_id}"
        resp = self.client.post(f"{url}/admin/fault", json={
            "point": point, "action": action, "delay_ms": delay_ms, "once": not always,
        })
        if resp.status_code == 200:
            when = "every time" if always else "once"
            return f"SUCCESS: Fault armed on Server {server_id}: {point} → {action} ({when})"
        return f"FAILED: {resp.text}"

    def _parallel(self, line: str) -> str:
        """PARALLEL WITHDRAW A101 8000 ; WITHDRAW A101 8000 runs both at the same instant."""
        commands = [c.strip() for c in line.strip()[len("PARALLEL"):].split(";") if c.strip()]
        if len(commands) < 2:
            return "Usage: PARALLEL <command> ; <command> [; ...]"
        results: list[str] = [""] * len(commands)
        barrier = threading.Barrier(len(commands))

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = self.run_command(commands[i])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(commands))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return "\n".join(f"  T{i + 1} {cmd}\n     → {res}"
                         for i, (cmd, res) in enumerate(zip(commands, results)))

    def _format(self, resp: httpx.Response) -> str:
        """Format an API response for display."""
        try:
            data = resp.json()
        except Exception:
            if resp.status_code >= 400:
                return f"FAILED: HTTP {resp.status_code}"
            return f"SUCCESS: HTTP {resp.status_code}"

        status = data.get("status", "")
        message = data.get("message", "")
        txn_id = data.get("txn_id", "")
        detail = data.get("detail", "")

        if resp.status_code >= 400:
            reason = message or detail or f"HTTP {resp.status_code}"
            if not isinstance(reason, str):
                reason = str(reason)
            out = f"FAILED: {reason}"
            if txn_id:
                out += f"  [txn: {txn_id}]"
            return out

        if status == "PENDING":
            out = f"PENDING: {message}"
        elif status == "FAILED":
            out = f"FAILED: {message}"
        else:
            out = f"SUCCESS: {message}"

        if txn_id:
            out += f"  [txn: {txn_id}]"

        # Show balance/data info
        extra = data.get("data", {})
        if extra:
            bal = extra.get("balance")
            balances = extra.get("balances")
            if bal is not None and f"₹{bal}" not in message:
                out += f"  Balance: ₹{bal}"
            if balances:
                bal_str = ", ".join(f"{k}=₹{v}" for k, v in balances.items())
                out += f"  Balances: {bal_str}"

        return out

    def _help(self) -> str:
        return """Available commands:
  CREATE <account_id> <owner> <initial_balance>   - Create an account
  DEPOSIT <account_id> <amount>                    - Deposit to an account
  WITHDRAW <account_id> <amount>                   - Withdraw from an account
  BALANCE <account_id>                             - Check account balance
  TRANSFER <source> <destination> <amount>         - Transfer between accounts
  HISTORY <account_id>                             - Transaction history for an account
  ACCOUNTS                                         - All accounts with owner, server and balance
  LOG [n]                                          - Last n global log entries (default 20)
  STATUS <txn_id>                                  - Check status of a transaction
  HEALTH                                           - System health check
  RESET                                            - Wipe all servers and re-seed accounts
  FAULT <server_id> <point> <action> [delay_ms] [always]
                                                   - Inject a fault (fires once unless 'always')
  PARALLEL <cmd> ; <cmd> [; ...]                   - Run commands concurrently
  HELP                                             - Show this help
  EXIT / QUIT                                      - Exit the client"""


def main():
    parser = argparse.ArgumentParser(description="Distributed Bank Client")
    parser.add_argument("--gateway", type=str, default=DEFAULT_GATEWAY, help="Gateway URL")
    parser.add_argument("--script", type=str, default=None, help="Script file to execute")
    args = parser.parse_args()

    client = BankClient(args.gateway)

    if args.script:
        # Batch mode: read and execute each line
        with open(args.script) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                print(f"bank> {line}")
                result = client.run_command(line)
                if result == "__EXIT__":
                    break
                print(result)
                print()
    else:
        # Interactive mode
        print("Distributed Bank Transaction System — Interactive Client")
        print(f"Connected to gateway at {args.gateway}")
        print("Type HELP for available commands, EXIT to quit.\n")
        while True:
            try:
                line = input("bank> ")
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            result = client.run_command(line)
            if result == "__EXIT__":
                print("Goodbye!")
                break
            if result:
                print(result)
            print()


if __name__ == "__main__":
    main()
