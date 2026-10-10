"""Request and response models."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Any, Optional


class CreateAccountRequest(BaseModel):
    account_id: str = Field(..., pattern=r"^A[1-9]\d{2}$", description="Account ID, e.g. A101")
    owner: str = Field(..., min_length=1, description="Account owner name")
    initial_balance: int = Field(0, ge=0, description="Initial balance in whole rupees")
    txn_id: str = Field("", description="Set by the gateway so server and global logs share the ID")


class DepositRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Amount to deposit (positive integer)")


class WithdrawRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Amount to withdraw (positive integer)")


class TransferRequest(BaseModel):
    source: str = Field(..., pattern=r"^A[1-9]\d{2}$", description="Source account ID")
    destination: str = Field(..., pattern=r"^A[1-9]\d{2}$", description="Destination account ID")
    amount: int = Field(..., gt=0, description="Amount to transfer (positive integer)")


class LocalTransferRequest(BaseModel):
    """Used internally for same-server transfers."""
    txn_id: str
    source: str
    destination: str
    amount: int


class PrepareRequest(BaseModel):
    """2PC Phase 1: prepare a debit or credit."""
    txn_id: str
    account_id: str
    op: str = Field(..., pattern=r"^(debit|credit)$")
    amount: int = Field(..., gt=0)


class CommitRequest(BaseModel):
    """2PC Phase 2: commit a prepared transaction."""
    txn_id: str


class AbortRequest(BaseModel):
    """2PC Phase 2: abort a prepared transaction."""
    txn_id: str


class FaultRequest(BaseModel):
    """Inject a fault for testing."""
    point: str = Field(..., description="Hook point name, e.g. on_prepare_before_vote")
    action: str = Field(..., pattern=r"^(crash|delay)$")
    delay_ms: int = Field(0, ge=0, description="Delay in milliseconds (for action=delay)")
    once: bool = Field(True, description="If true, fault fires only once then disarms")


class ResetRequest(BaseModel):
    seed: bool = Field(True, description="If true, re-seed default accounts after wipe")


class ServerDepositRequest(BaseModel):
    """Internal deposit request used by bank servers (includes txn_id)."""
    amount: int = Field(..., gt=0)
    txn_id: str = Field("", description="Transaction ID for logging")


class ServerWithdrawRequest(BaseModel):
    """Internal withdraw request used by bank servers (includes txn_id)."""
    amount: int = Field(..., gt=0)
    txn_id: str = Field("", description="Transaction ID for logging")


class ApiResponse(BaseModel):
    """Uniform response envelope for all API endpoints."""
    txn_id: str = ""
    status: str = "SUCCESS"            # SUCCESS | FAILED | PENDING
    message: str = ""
    data: Optional[dict[str, Any]] = None


class VoteResponse(BaseModel):
    """Response from a 2PC prepare call."""
    txn_id: str
    vote: str                           # VOTE_COMMIT | VOTE_ABORT
    reason: str = ""

