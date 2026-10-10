"""Gateway: public API, routing to bank servers, coordination of cross-server transfers.

    python -m gateway.gateway --port 8000
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager

import anyio.to_thread
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from common.models import (
    CreateAccountRequest, DepositRequest, WithdrawRequest,
    TransferRequest, ResetRequest, ApiResponse,
)
from common.errors import (
    source_not_found_msg, dest_not_found_msg, server_unavailable_msg,
)
from gateway.router import route
from gateway.txn_log import TxnLog
from gateway.coordinator import Coordinator, generate_txn_id
from gateway.recovery import GatewayRecovery

logger = logging.getLogger(__name__)

txn_log: TxnLog
coordinator: Coordinator
gw_recovery: GatewayRecovery
http_client: httpx.Client


def create_app(db_path: Path | None = None, jsonl_path: Path | None = None) -> FastAPI:
    """Create and configure the gateway FastAPI app."""
    global txn_log, coordinator, http_client

    config.ensure_dirs()
    if db_path is None:
        db_path = config.DATA_DIR / "gateway.db"
    if jsonl_path is None:
        jsonl_path = config.LOG_DIR / "transactions.jsonl"

    txn_log = TxnLog(db_path, jsonl_path)
    # Transfers left undecided by a previous coordinator crash are aborted
    # before we serve any /2pc/decision query.
    aborted = txn_log.abort_undecided()
    if aborted:
        logger.warning(f"Aborted {aborted} transfer(s) left undecided by a coordinator crash")

    http_client = httpx.Client(
        timeout=httpx.Timeout(config.CONNECT_TIMEOUT, read=config.READ_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    coordinator = Coordinator(txn_log, http_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global gw_recovery
        anyio.to_thread.current_default_thread_limiter().total_tokens = config.THREAD_POOL_SIZE
        gw_recovery = GatewayRecovery(coordinator, config.RECOVERY_RETRY_INTERVAL)
        gw_recovery.start()
        logger.info("Gateway started")
        yield
        gw_recovery.stop()
        http_client.close()
        logger.info("Gateway shutting down")

    app = FastAPI(title="Distributed Bank Gateway", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors())
        return _failed(400, f"Invalid request: {problems}")

    _register_routes(app)
    return app


def _failed(status_code: int, message: str, txn_id: str = "") -> JSONResponse:
    """Uniform FAILED response. `detail` mirrors `message` for FastAPI-style clients."""
    return JSONResponse(
        status_code=status_code,
        content={"txn_id": txn_id, "status": "FAILED", "message": message,
                 "detail": message, "data": None},
    )


def _upstream_detail(resp: httpx.Response, default_msg: str = "Server error") -> str:
    """Extract the error message from a non-200 bank server response."""
    try:
        return resp.json().get("detail", default_msg)
    except Exception:
        return default_msg


def _transport_failure(exc: httpx.TransportError, server_id: int) -> tuple[int, str]:
    """Connection refused means the request never arrived. Anything else
    (timeout, dropped connection) means it may or may not have been applied.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return 503, server_unavailable_msg(server_id)
    return 504, f"Server {server_id} did not respond; outcome unknown, check BALANCE"


def _register_routes(app: FastAPI) -> None:
    """Register all gateway routes."""

    # CREATE

    @app.post("/accounts")
    def create_account(req: CreateAccountRequest):
        txn_id = generate_txn_id()

        def fail(code: int, reason: str) -> JSONResponse:
            txn_log.log(txn_id=txn_id, txn_type="CREATE", amount=req.initial_balance,
                        status="FAILED", source=req.account_id, failure_reason=reason)
            return _failed(code, reason, txn_id)

        sid, url = route(req.account_id)
        if url is None:
            return fail(400, "Invalid account ID: no server for this prefix")

        body = req.model_dump()
        body["txn_id"] = txn_id
        try:
            resp = http_client.post(f"{url}/accounts", json=body)
        except httpx.TransportError as e:
            return fail(*_transport_failure(e, sid))

        if resp.status_code != 200:
            return fail(resp.status_code, _upstream_detail(resp))

        data = resp.json()
        data["txn_id"] = txn_id
        txn_log.log(
            txn_id=txn_id, txn_type="CREATE", amount=req.initial_balance,
            status="SUCCESS", source=req.account_id,
        )
        return data

    # DEPOSIT / WITHDRAW

    def single_account_op(txn_type: str, path: str, account_id: str, amount: int):
        """Forward a deposit/withdraw to the owning server and log the outcome."""
        txn_id = generate_txn_id()

        def fail(code: int, reason: str) -> JSONResponse:
            txn_log.log(txn_id=txn_id, txn_type=txn_type, amount=amount,
                        status="FAILED", source=account_id, failure_reason=reason)
            return _failed(code, reason, txn_id)

        sid, url = route(account_id)
        if url is None:
            return fail(404, "Account not found")

        try:
            resp = http_client.post(
                f"{url}/accounts/{account_id}/{path}",
                json={"amount": amount, "txn_id": txn_id},
            )
        except httpx.TransportError as e:
            return fail(*_transport_failure(e, sid))

        if resp.status_code != 200:
            return fail(resp.status_code, _upstream_detail(resp))

        data = resp.json()
        data["txn_id"] = txn_id
        txn_log.log(
            txn_id=txn_id, txn_type=txn_type, amount=amount,
            status="SUCCESS", source=account_id,
        )
        return data

    @app.post("/accounts/{account_id}/deposit")
    def deposit(account_id: str, req: DepositRequest):
        return single_account_op("DEPOSIT", "deposit", account_id, req.amount)

    @app.post("/accounts/{account_id}/withdraw")
    def withdraw(account_id: str, req: WithdrawRequest):
        return single_account_op("WITHDRAW", "withdraw", account_id, req.amount)

    # BALANCE

    @app.get("/accounts")
    def list_accounts():
        """All accounts on all servers, for the ACCOUNTS command."""
        accounts, unreachable = [], []
        for sid, url in config.SERVERS.items():
            try:
                resp = http_client.get(f"{url}/accounts")
                for a in resp.json()["accounts"]:
                    accounts.append({**a, "server": sid})
            except httpx.TransportError:
                unreachable.append(sid)
        return ApiResponse(status="SUCCESS", data={"accounts": accounts, "unreachable": unreachable})

    @app.get("/accounts/{account_id}/balance")
    def balance(account_id: str):
        sid, url = route(account_id)
        if url is None:
            return _failed(404, "Account not found")

        try:
            resp = http_client.get(f"{url}/accounts/{account_id}")
        except httpx.TransportError as e:
            return _failed(*_transport_failure(e, sid))

        if resp.status_code != 200:
            return _failed(resp.status_code, _upstream_detail(resp))
        return resp.json()

    # TRANSFER

    @app.post("/transfers")
    def transfer(req: TransferRequest):
        txn_id = generate_txn_id()

        def fail(code: int, reason: str) -> JSONResponse:
            txn_log.log(txn_id=txn_id, txn_type="TRANSFER", amount=req.amount,
                        status="FAILED", source=req.source, destination=req.destination,
                        failure_reason=reason)
            return _failed(code, reason, txn_id)

        # Validation
        if req.source == req.destination:
            return fail(400, "Cannot transfer to the same account")

        src_sid, src_url = route(req.source)
        dst_sid, dst_url = route(req.destination)

        if src_url is None:
            return fail(404, source_not_found_msg())
        if dst_url is None:
            return fail(404, dest_not_found_msg())

        # Same server → local transfer (no 2PC needed)
        if src_sid == dst_sid:
            try:
                resp = http_client.post(
                    f"{src_url}/local-transfer",
                    json={
                        "txn_id": txn_id,
                        "source": req.source,
                        "destination": req.destination,
                        "amount": req.amount,
                    },
                )
            except httpx.TransportError as e:
                return fail(*_transport_failure(e, src_sid))

            if resp.status_code != 200:
                return fail(resp.status_code, _upstream_detail(resp))

            data = resp.json()
            data["txn_id"] = txn_id
            txn_log.log(
                txn_id=txn_id, txn_type="TRANSFER", amount=req.amount,
                status="SUCCESS", source=req.source, destination=req.destination,
            )
            return data

        # Cross-server → 2PC (the coordinator writes the log record itself)
        result = coordinator.transfer(txn_id, req.source, req.destination, req.amount)
        status_code = {"SUCCESS": 200, "FAILED": 409, "PENDING": 202}.get(result["status"], 500)
        if result["status"] == "FAILED" and "not found" in result.get("message", "").lower():
            status_code = 404
        if result["status"] == "FAILED" and "unavailable" in result.get("message", "").lower():
            status_code = 503
        if result["status"] == "FAILED" and "did not respond" in result.get("message", ""):
            status_code = 504

        content = {
            "txn_id": txn_id,
            "status": result["status"],
            "message": result["message"],
            "data": result.get("data"),
        }
        if result["status"] == "FAILED":
            content["detail"] = result["message"]
        return JSONResponse(status_code=status_code, content=content)

    # Transaction History (per account)

    @app.get("/accounts/{account_id}/transactions")
    def account_history(account_id: str):
        sid, url = route(account_id)
        if url is None:
            return _failed(404, "Account not found")
        try:
            resp = http_client.get(f"{url}/accounts/{account_id}/transactions")
        except httpx.TransportError as e:
            return _failed(*_transport_failure(e, sid))
        if resp.status_code != 200:
            return _failed(resp.status_code, _upstream_detail(resp))
        return resp.json()

    # Global Transaction Log

    @app.get("/transactions")
    def list_transactions(
        status: str = Query("", description="Filter by status"),
        limit: int = Query(100, ge=1, le=1000),
    ):
        txns = txn_log.list_transactions(status=status, limit=limit)
        return ApiResponse(status="SUCCESS", data={"transactions": txns})

    @app.get("/transactions/{txn_id}")
    def get_transaction(txn_id: str):
        txn = txn_log.get(txn_id)
        if txn is None:
            return _failed(404, "Transaction not found")
        return ApiResponse(txn_id=txn_id, status=txn["status"],
                           message=txn.get("failure_reason") or "", data=txn)

    # 2PC Decision endpoint (for participant recovery)

    @app.get("/2pc/decision/{txn_id}")
    def get_decision(txn_id: str):
        txn = txn_log.get(txn_id)
        if txn is None:
            # Presumed abort: if coordinator has no record, the txn never committed
            raise HTTPException(404, detail="No decision found (presumed abort)")
        # PENDING = still in phase 1; the participant must wait, not abort
        return {"txn_id": txn_id, "decision": txn["decision"] or "PENDING"}

    # Health

    @app.get("/health")
    def health():
        server_status = {}
        for sid, url in config.SERVERS.items():
            try:
                resp = http_client.get(f"{url}/health", timeout=1.0)
                server_status[f"server_{sid}"] = "ok" if resp.status_code == 200 else "error"
            except Exception:
                server_status[f"server_{sid}"] = "unreachable"
        return {
            "status": "ok",
            "servers": server_status,
        }

    # Admin Reset

    @app.post("/admin/reset")
    def reset(req: ResetRequest = ResetRequest()):
        txn_log.reset()
        errors = []
        for sid, url in config.SERVERS.items():
            try:
                resp = http_client.post(
                    f"{url}/admin/reset",
                    json={"seed": req.seed},
                    timeout=3.0,
                )
                if resp.status_code != 200:
                    errors.append(f"Server {sid}: {resp.status_code}")
            except Exception as e:
                errors.append(f"Server {sid}: {e}")
        return {
            "status": "reset",
            "seeded": req.seed,
            "errors": errors,
        }


def main():
    parser = argparse.ArgumentParser(description="Distributed Bank Gateway")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--data-dir", type=str, default=None)
    args = parser.parse_args()

    if args.data_dir:
        config.DATA_DIR = Path(args.data_dir)
        if "BANK_LOG_DIR" not in os.environ:
            config.LOG_DIR = Path(args.data_dir).parent / "logs"
    config.ensure_dirs()

    logging.basicConfig(
        level=logging.INFO,
        format="[Gateway] %(asctime)s %(levelname)s %(message)s",
    )

    port = args.port or config.GATEWAY_PORT
    app = create_app()

    import uvicorn
    uvicorn.run(app, host=args.host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
