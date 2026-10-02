"""A local stand-in for Zoho's accounts server and the Inventory v1 API.

It exists so reviewers can run the full connector -- OAuth consent, token refresh, paging,
search, rate limiting -- without a Zoho account. It mirrors the behaviour the connector relies
on (response envelopes, page_context, error codes, 401 on expired tokens, 429 when throttled)
and serves the fictional dataset in data.py. It is a test double, not a Zoho emulator: only the
endpoints and filters this connector uses are implemented.

Fault injection for demos/tests:  POST /__mock/faults {"fail_next": [429, 500], "expire_tokens": true}
"""

from __future__ import annotations

import html
import os
import secrets
import time
import urllib.parse
from collections import deque
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .data import build_dataset

NOT_AUTHORIZED = {"code": 57, "message": "You are not authorized to perform this operation"}


class MockZoho:
    def __init__(
        self,
        *,
        per_minute: int | None = None,
        daily_limit: int | None = None,
        token_ttl: int | None = None,
    ) -> None:
        self.data = build_dataset()
        self.per_minute = per_minute or int(os.environ.get("MOCK_RATE_PER_MINUTE", "100"))
        self.daily_limit = daily_limit or int(os.environ.get("MOCK_DAILY_LIMIT", "1000"))
        self.token_ttl = token_ttl or int(os.environ.get("MOCK_TOKEN_TTL", "3600"))
        self.reset()

    def reset(self) -> None:
        self.codes: dict[str, str] = {}  # auth code -> client_id
        self.refresh_tokens: dict[str, str] = {}  # refresh -> client_id
        self.access_tokens: dict[str, float] = {}  # access -> expiry
        self.window: deque[float] = deque()
        self.daily_used = 0
        self.fail_next: list[int] = []
        self.request_log: list[str] = []

    # ---------- OAuth ----------

    def base_url(self, request: Request) -> str:
        return f"{request.url.scheme}://{request.url.netloc}"

    async def authorize(self, request: Request) -> Response:
        q = request.query_params
        missing = [k for k in ("client_id", "redirect_uri", "scope", "response_type") if not q.get(k)]
        if missing:
            return HTMLResponse(f"<h3>invalid request: missing {', '.join(missing)}</h3>", status_code=400)
        scopes = "".join(f"<li><code>{html.escape(s)}</code></li>" for s in q["scope"].split(","))
        approve = "/oauth/v2/approve?" + urllib.parse.urlencode(dict(q))
        deny = q["redirect_uri"] + "?" + urllib.parse.urlencode({"error": "access_denied", "state": q.get("state", "")})
        return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>Mock Zoho consent</title>
<body style="font-family:system-ui;max-width:34rem;margin:3rem auto;line-height:1.5">
<p style="color:#b45309"><b>MOCK</b> Zoho accounts server (local demo, no real account)</p>
<h2>Allow "Agent Studio connector" to access {html.escape(self.data['organization']['name'])}?</h2>
<p>Requested permissions (read-only):</p><ul>{scopes}</ul>
<p><a href="{html.escape(approve)}" style="padding:.5rem 1rem;background:#0b6;color:#fff;border-radius:6px;text-decoration:none">Accept</a>
&nbsp; <a href="{html.escape(deny)}">Reject</a></p></body>""")

    async def approve(self, request: Request) -> Response:
        q = request.query_params
        code = "1000." + secrets.token_hex(16)
        self.codes[code] = q["client_id"]
        params = {"code": code, "state": q.get("state", ""), "location": "in", "accounts-server": self.base_url(request)}
        return RedirectResponse(q["redirect_uri"] + "?" + urllib.parse.urlencode(params), status_code=302)

    def issue_code(self, client_id: str) -> str:
        """Used by the zero-setup demo mode to simulate the merchant clicking Accept."""
        code = "1000." + secrets.token_hex(16)
        self.codes[code] = client_id
        return code

    def _access(self) -> tuple[str, int]:
        token = "1000." + secrets.token_hex(24)
        self.access_tokens[token] = time.time() + self.token_ttl
        return token, self.token_ttl

    async def token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        api_domain = self.base_url(request)
        if grant == "authorization_code":
            if self.codes.pop(str(form.get("code")), None) is None:
                return JSONResponse({"error": "invalid_code"})  # Zoho answers 200 with an error field
            refresh = "1000." + secrets.token_hex(24)
            self.refresh_tokens[refresh] = str(form.get("client_id"))
            access, ttl = self._access()
            return JSONResponse({"access_token": access, "refresh_token": refresh, "api_domain": api_domain, "token_type": "Bearer", "expires_in": ttl})
        if grant == "refresh_token":
            if str(form.get("refresh_token")) not in self.refresh_tokens:
                return JSONResponse({"error": "invalid_code"})
            access, ttl = self._access()
            return JSONResponse({"access_token": access, "api_domain": api_domain, "token_type": "Bearer", "expires_in": ttl})
        return JSONResponse({"error": "unsupported_grant_type"})

    async def revoke(self, request: Request) -> Response:
        self.refresh_tokens.pop(request.query_params.get("token", ""), None)
        return JSONResponse({"status": "success"})

    # ---------- API plumbing ----------

    def _guard(self, request: Request, org_scoped: bool = True) -> Response | None:
        self.request_log.append(request.url.path)
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Zoho-oauthtoken ").strip()
        expiry = self.access_tokens.get(token)
        if not expiry or expiry < time.time():
            return JSONResponse(NOT_AUTHORIZED, status_code=401)
        if self.fail_next:
            status = self.fail_next.pop(0)
            if status == 429:
                return JSONResponse({"code": 44, "message": "You have made too many requests. Please try again after some time."}, status_code=429, headers={"Retry-After": "1"})
            if status == 1070:
                return JSONResponse({"code": 1070, "message": "Concurrent request limit exceeded"}, status_code=429)
            return JSONResponse({"code": 1, "message": "Internal error"}, status_code=status)
        now = time.monotonic()
        while self.window and now - self.window[0] >= 60:
            self.window.popleft()
        if self.daily_used >= self.daily_limit:
            return JSONResponse({"code": 45, "message": "You have reached the maximum number of API requests allowed per day."}, status_code=429)
        if len(self.window) >= self.per_minute:
            retry = int(60 - (now - self.window[0])) + 1
            return JSONResponse({"code": 44, "message": "You have made too many requests. Please try again after some time."}, status_code=429, headers={"Retry-After": str(retry)})
        self.window.append(now)
        self.daily_used += 1
        if org_scoped and request.query_params.get("organization_id") != self.data["organization"]["organization_id"]:
            return JSONResponse({"code": 6041, "message": "Invalid value passed for organization_id"}, status_code=400)
        return None

    @staticmethod
    def _paginate(rows: list[dict[str, Any]], request: Request, key: str, extra: dict[str, Any] | None = None) -> JSONResponse:
        q = request.query_params
        page = max(1, int(q.get("page", 1)))
        per_page = min(200, max(1, int(q.get("per_page", 200))))
        start = (page - 1) * per_page
        chunk = rows[start : start + per_page]
        return JSONResponse({
            "code": 0,
            "message": "success",
            key: chunk,
            "page_context": {"page": page, "per_page": per_page, "has_more_page": start + per_page < len(rows), **(extra or {})},
        })

    # ---------- API endpoints ----------

    async def organizations(self, request: Request) -> Response:
        if (err := self._guard(request, org_scoped=False)) is not None:
            return err
        return JSONResponse({"code": 0, "message": "success", "organizations": [self.data["organization"]]})

    async def list_items(self, request: Request) -> Response:
        if (err := self._guard(request)) is not None:
            return err
        q = request.query_params
        rows = list(self.data["items"])
        f = q.get("filter_by", "Status.All")
        if f == "Status.Active":
            rows = [r for r in rows if r["status"] == "active"]
        elif f == "Status.Inactive":
            rows = [r for r in rows if r["status"] == "inactive"]
        elif f == "Status.Lowstock":
            rows = [r for r in rows if r["actual_available_stock"] <= r["reorder_level"]]
        for field in ("sku", "name"):
            if v := q.get(field):
                rows = [r for r in rows if r[field].lower() == v.lower()]
            if v := q.get(f"{field}_contains"):
                rows = [r for r in rows if v.lower() in r[field].lower()]
            if v := q.get(f"{field}_startswith"):
                rows = [r for r in rows if r[field].lower().startswith(v.lower())]
        if v := q.get("search_text"):
            rows = [r for r in rows if v.lower() in r["name"].lower() or v.lower() in r["sku"].lower()]
        sort = q.get("sort_column", "name")
        if sort in ("name", "sku", "stock_on_hand", "last_modified_time"):
            rows.sort(key=lambda r: r[sort], reverse=q.get("sort_order") == "D")
        # The list endpoint does not include per-location stock (same as Zoho).
        rows = [{k: v for k, v in r.items() if k != "locations"} for r in rows]
        return self._paginate(rows, request, "items")

    async def get_item(self, request: Request) -> Response:
        if (err := self._guard(request)) is not None:
            return err
        item = next((i for i in self.data["items"] if i["item_id"] == request.path_params["item_id"]), None)
        if item is None:
            return JSONResponse({"code": 1002, "message": "Item does not exist."}, status_code=404)
        return JSONResponse({"code": 0, "message": "success", "item": item})

    async def list_salesorders(self, request: Request) -> Response:
        if (err := self._guard(request)) is not None:
            return err
        q = request.query_params
        rows = list(self.data["salesorders"])
        f = q.get("filter_by", "Status.All")
        if f != "Status.All":
            wanted = f.removeprefix("Status.").lower()
            rows = [r for r in rows if r["status"] == wanted]
        for field in ("reference_number", "salesorder_number"):
            if v := q.get(field):
                rows = [r for r in rows if r[field].lower() == v.lower()]
        # Same semantics as a live org: customer_name is exact, *_contains / *_startswith are partial.
        if v := q.get("customer_name"):
            rows = [r for r in rows if r["customer_name"].lower() == v.lower()]
        if v := q.get("customer_name_contains"):
            rows = [r for r in rows if v.lower() in r["customer_name"].lower()]
        if v := q.get("customer_name_startswith"):
            rows = [r for r in rows if r["customer_name"].lower().startswith(v.lower())]
        if v := q.get("search_text"):
            v = v.lower()
            rows = [r for r in rows if v in r["salesorder_number"].lower() or v in r["reference_number"].lower()
                    or v in r["customer_name"].lower() or any(v in li["name"].lower() or v in li["sku"].lower() for li in r["line_items"])]
        if v := q.get("date_start"):
            rows = [r for r in rows if r["date"] >= v]
        if v := q.get("date_end"):
            rows = [r for r in rows if r["date"] <= v]
        rows.sort(key=lambda r: (r["date"], r["salesorder_number"]), reverse=q.get("sort_order", "D") == "D")
        list_fields = ("salesorder_id", "salesorder_number", "date", "status", "order_status", "reference_number", "customer_id",
                       "customer_name", "currency_code", "total", "shipped_status", "invoiced_status", "paid_status",
                       "delivery_method", "created_time", "last_modified_time")
        rows = [{k: r[k] for k in list_fields} for r in rows]
        return self._paginate(rows, request, "salesorders")

    async def get_salesorder(self, request: Request) -> Response:
        if (err := self._guard(request)) is not None:
            return err
        so = next((s for s in self.data["salesorders"] if s["salesorder_id"] == request.path_params["so_id"]), None)
        if so is None:
            return JSONResponse({"code": 1002, "message": "Sales Order does not exist."}, status_code=404)
        return JSONResponse({"code": 0, "message": "success", "salesorder": so})

    async def get_package(self, request: Request) -> Response:
        if (err := self._guard(request)) is not None:
            return err
        pkg = next((p for p in self.data["packages"] if p["package_id"] == request.path_params["package_id"]), None)
        if pkg is None:
            return JSONResponse({"code": 1002, "message": "Package does not exist."}, status_code=404)
        return JSONResponse({"code": 0, "message": "success", "package": pkg})

    # ---------- test controls ----------

    async def faults(self, request: Request) -> Response:
        body = await request.json()
        self.fail_next.extend(int(s) for s in body.get("fail_next", []))
        if body.get("expire_tokens"):
            self.access_tokens = {t: 0.0 for t in self.access_tokens}
        if body.get("reset_limits"):
            self.window.clear()
            self.daily_used = 0
        return JSONResponse({"fail_next": self.fail_next, "daily_used": self.daily_used})

    def app(self) -> Starlette:
        api = "/inventory/v1"
        return Starlette(routes=[
            Route("/oauth/v2/auth", self.authorize),
            Route("/oauth/v2/approve", self.approve),
            Route("/oauth/v2/token", self.token, methods=["POST"]),
            Route("/oauth/v2/token/revoke", self.revoke, methods=["POST"]),
            Route(f"{api}/organizations", self.organizations),
            Route(f"{api}/items", self.list_items),
            Route(f"{api}/items/{{item_id}}", self.get_item),
            Route(f"{api}/salesorders", self.list_salesorders),
            Route(f"{api}/salesorders/{{so_id}}", self.get_salesorder),
            Route(f"{api}/packages/{{package_id}}", self.get_package),
            Route("/__mock/faults", self.faults, methods=["POST"]),
            Route("/", lambda r: JSONResponse({"service": "mock zoho", "org": self.data["organization"]["name"]})),
        ])


def run(host: str = "127.0.0.1", port: int = 8900) -> None:
    import uvicorn

    print(f"Mock Zoho (accounts + Inventory API) on http://{host}:{port}  -- fictional data only")
    uvicorn.run(MockZoho().app(), host=host, port=port, log_level="warning")
