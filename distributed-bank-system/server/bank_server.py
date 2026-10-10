"""Bank server. One process per server:

    python -m server.bank_server --id 1 --port 8001
"""

import argparse
import logging
import sys
from pathlib import Path
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, HTTPException

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from common.models import (
    CreateAccountRequest, ServerDepositRequest, ServerWithdrawRequest,
    LocalTransferRequest, PrepareRequest, CommitRequest, AbortRequest,
    FaultRequest, ResetRequest, ApiResponse, VoteResponse,
)
from server.store import Store
from server.locks import LockManager
from server.faults import FaultInjector
from server.recovery import ServerRecovery

logger = logging.getLogger(__name__)

server_id: int = 0
store: Store
lock_manager: LockManager = LockManager()
fault_injector: FaultInjector = FaultInjector()
recovery: ServerRecovery | None = None


def create_app(sid: int, db_path: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI app for a bank server."""
    global server_id, store

    server_id = sid

    if db_path is None:
        config.ensure_dirs()
        db_path = config.DATA_DIR / f"server{sid}.db"

    is_new_db = not db_path.exists()
    store = Store(db_path)
    if is_new_db:
        # First start on this machine: seed the default accounts for this server
        store.seed(_my_seed_accounts())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: size the request thread pool, start in-doubt txn recovery
        global recovery
        anyio.to_thread.current_default_thread_limiter().total_tokens = config.THREAD_POOL_SIZE
        gateway_url = f"http://{config.GATEWAY_HOST}:{config.GATEWAY_PORT}"
        recovery = ServerRecovery(store, gateway_url, config.RECOVERY_RETRY_INTERVAL,
                                  config.IN_DOUBT_TIMEOUT)
        recovery.start()
        logger.info(f"Bank Server {server_id} started (db: {db_path})")
        yield
        # Shutdown
        if recovery:
            recovery.stop()
        logger.info(f"Bank Server {server_id} shutting down")

    app = FastAPI(title=f"Bank Server {sid}", lifespan=lifespan)
    _register_routes(app)
    return app


def _my_seed_accounts() -> list[dict]:
    """Seed accounts that belong to this server."""
    return [a for a in config.SEED_ACCOUNTS if config.server_for(a["account_id"]) == server_id]


def _register_routes(app: FastAPI) -> None:
    """Register all routes on the app."""

    # Account CRUD

    @app.post("/accounts")
    def create_account(req: CreateAccountRequest):
        fault_injector.check("on_request")
        with lock_manager.lock(req.account_id):
            ok = store.create_account(req.account_id, req.owner, req.initial_balance)
        if not ok:
            raise HTTPException(409, detail="Account already exists")
        txn_id = req.txn_id or f"CREATE-{req.account_id}"
        store.append_log(
            txn_id=txn_id,
            account_id=req.account_id,
            txn_type="CREATE",
            amount=req.initial_balance,
            balance_after=req.initial_balance,
            status="SUCCESS",
        )
        return ApiResponse(
            txn_id=txn_id,
            status="SUCCESS",
            message=f"Account {req.account_id} created with balance ₹{req.initial_balance}",
            data={"account_id": req.account_id, "balance": req.initial_balance},
        )

    @app.post("/accounts/{account_id}/deposit")
    def deposit(account_id: str, req: ServerDepositRequest):
        fault_injector.check("on_request")
        with lock_manager.lock(account_id):
            acct = store.get_account(account_id)
            if acct is None:
                raise HTTPException(404, detail="Account not found")
            new_bal = store.update_balance(account_id, req.amount)
        store.append_log(req.txn_id, account_id, "DEPOSIT", req.amount, new_bal, "SUCCESS")
        return ApiResponse(
            txn_id=req.txn_id,
            status="SUCCESS",
            message=f"₹{req.amount} deposited to {account_id}",
            data={"account_id": account_id, "balance": new_bal},
        )

    @app.post("/accounts/{account_id}/withdraw")
    def withdraw(account_id: str, req: ServerWithdrawRequest):
        fault_injector.check("on_request")
        with lock_manager.lock(account_id):
            acct = store.get_account(account_id)
            if acct is None:
                raise HTTPException(404, detail="Account not found")
            avail = acct["balance"] - acct["held"]
            if avail < req.amount:
                store.append_log(
                    req.txn_id, account_id, "WITHDRAW", req.amount,
                    acct["balance"], "FAILED", "Insufficient balance",
                )
                raise HTTPException(409, detail="Insufficient balance")
            new_bal = store.update_balance(account_id, -req.amount)
        store.append_log(req.txn_id, account_id, "WITHDRAW", req.amount, new_bal, "SUCCESS")
        return ApiResponse(
            txn_id=req.txn_id,
            status="SUCCESS",
            message=f"₹{req.amount} withdrawn from {account_id}",
            data={"account_id": account_id, "balance": new_bal},
        )

    @app.get("/accounts")
    def list_accounts():
        return {"server_id": server_id, "accounts": store.list_accounts()}

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        fault_injector.check("on_request")
        acct = store.get_account(account_id)
        if acct is None:
            raise HTTPException(404, detail="Account not found")
        return ApiResponse(
            status="SUCCESS",
            message=f"Balance of {account_id}: ₹{acct['balance']}",
            data=acct,
        )

    @app.get("/accounts/{account_id}/transactions")
    def get_history(account_id: str):
        fault_injector.check("on_request")
        acct = store.get_account(account_id)
        if acct is None:
            raise HTTPException(404, detail="Account not found")
        history = store.get_history(account_id)
        return ApiResponse(
            status="SUCCESS",
            data={"account_id": account_id, "transactions": history},
        )

    # Local Transfer

    @app.post("/local-transfer")
    def local_transfer(req: LocalTransferRequest):
        fault_injector.check("on_request")
        with lock_manager.multi_lock(req.source, req.destination):
            ok, reason, balances = store.local_transfer(
                req.txn_id, req.source, req.destination, req.amount
            )
        if not ok:
            status_code = 409 if "Insufficient" in reason else 404
            raise HTTPException(status_code, detail=reason)
        return ApiResponse(
            txn_id=req.txn_id,
            status="SUCCESS",
            message=f"Transfer of ₹{req.amount} from {req.source} to {req.destination} completed",
            data={"balances": balances},
        )

    # 2PC Endpoints

    @app.post("/2pc/prepare")
    def prepare(req: PrepareRequest):
        fault_injector.check("on_request")
        with lock_manager.lock(req.account_id):
            if req.op == "debit":
                ok, reason = store.prepare_debit(req.txn_id, req.account_id, req.amount)
            else:
                ok, reason = store.prepare_credit(req.txn_id, req.account_id, req.amount)

        # Fire the fault AFTER persisting PREPARED but BEFORE replying
        fault_injector.check("on_prepare_before_vote")

        if ok:
            return VoteResponse(txn_id=req.txn_id, vote="VOTE_COMMIT")
        else:
            return VoteResponse(txn_id=req.txn_id, vote="VOTE_ABORT", reason=reason)

    @app.post("/2pc/commit")
    def commit(req: CommitRequest):
        fault_injector.check("on_commit_before_apply")
        fault_injector.check("on_request")
        ok = store.commit_txn(req.txn_id)
        if ok:
            return ApiResponse(txn_id=req.txn_id, status="SUCCESS", message="Committed")
        raise HTTPException(404, detail="Transaction not found or already aborted")

    @app.post("/2pc/abort")
    def abort(req: AbortRequest):
        fault_injector.check("on_request")
        if not store.abort_txn(req.txn_id):
            raise HTTPException(409, detail="Transaction already committed")
        return ApiResponse(txn_id=req.txn_id, status="SUCCESS", message="Aborted")

    # Health

    @app.get("/health")
    def health():
        return {"status": "ok", "server_id": server_id}

    # Admin

    @app.post("/admin/fault")
    def arm_fault(req: FaultRequest):
        fault_injector.arm(req.point, req.action, req.delay_ms, req.once)
        return {"status": "armed", "point": req.point, "action": req.action}

    @app.post("/admin/reset")
    def reset(req: ResetRequest = ResetRequest()):
        fault_injector.clear_all()
        store.reset()
        if req.seed:
            store.seed(_my_seed_accounts())
        return {"status": "reset", "server_id": server_id, "seeded": req.seed}

    @app.get("/admin/prepared")
    def list_prepared():
        """In-doubt 2PC transactions (state PREPARED) on this server."""
        return {"server_id": server_id, "prepared": store.get_prepared_txns()}


def main():
    parser = argparse.ArgumentParser(description="Bank Server")
    parser.add_argument("--id", type=int, required=True, help="Server ID (1, 2, or 3)")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--data-dir", type=str, default=None, help="Override data directory")
    args = parser.parse_args()

    if args.data_dir:
        config.DATA_DIR = Path(args.data_dir)
    config.ensure_dirs()

    port = args.port or config.SERVER_PORTS[args.id]
    db_path = config.DATA_DIR / f"server{args.id}.db"
    app = create_app(args.id, db_path)

    logging.basicConfig(
        level=logging.INFO,
        format=f"[Server {args.id}] %(asctime)s %(levelname)s %(message)s",
    )

    import uvicorn
    uvicorn.run(app, host=args.host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
