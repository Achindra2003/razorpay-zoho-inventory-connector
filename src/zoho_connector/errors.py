"""One error type with stable codes, so an agent can decide what to do next
(retry later, ask the merchant to reconnect, fix its arguments) without parsing prose."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    NOT_CONNECTED = "NOT_CONNECTED"  # no stored connection: merchant must run `zoho-connector connect`
    AUTH_EXPIRED = "AUTH_EXPIRED"  # refresh token revoked/expired: merchant must reconnect
    FORBIDDEN = "FORBIDDEN"  # token lacks the scope or the user lacks the permission
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    RATE_LIMITED = "RATE_LIMITED"  # per-minute or concurrency limit; retry after `retry_after_s`
    DAILY_QUOTA_EXHAUSTED = "DAILY_QUOTA_EXHAUSTED"  # plan's daily API quota used up
    UPSTREAM_ERROR = "UPSTREAM_ERROR"  # Zoho 5xx or unexpected response
    TIMEOUT = "TIMEOUT"


# What the agent should do about each error. Kept next to the codes so docs and runtime agree.
HINTS: dict[ErrorCode, str] = {
    ErrorCode.NOT_CONNECTED: "Zoho Inventory is not connected. Ask the merchant to connect it; do not retry.",
    ErrorCode.AUTH_EXPIRED: "The merchant's Zoho authorization expired or was revoked. Ask them to reconnect; do not retry.",
    ErrorCode.FORBIDDEN: "This connection is not allowed to read that data. Do not retry.",
    ErrorCode.NOT_FOUND: "No record matched. Check the identifier or use a search tool.",
    ErrorCode.INVALID_INPUT: "Fix the arguments and call again.",
    ErrorCode.RATE_LIMITED: "Zoho is throttling requests. Wait retry_after_s seconds before calling again.",
    ErrorCode.DAILY_QUOTA_EXHAUSTED: "The merchant's daily Zoho API quota is used up. Tell the merchant; retry after the reset time.",
    ErrorCode.UPSTREAM_ERROR: "Zoho returned an error. You may retry once later.",
    ErrorCode.TIMEOUT: "Zoho did not respond in time. You may retry once.",
}


class ConnectorError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after_s: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code.value, "message": self.message, "hint": HINTS[self.code]}
        if self.retry_after_s is not None:
            body["retry_after_s"] = round(self.retry_after_s, 1)
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"
