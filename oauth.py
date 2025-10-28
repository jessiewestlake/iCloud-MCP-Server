"""
Local OAuth 2.1 authorization server integration for the FastMCP iCloud server.

This module provides a file-backed OAuth provider that supports Dynamic Client
Registration and an interactive consent screen protected by a shared password.
It builds on FastMCP's OAuth primitives so the resulting endpoints comply with
the Model Context Protocol authorization specification.
"""

from __future__ import annotations

import asyncio
import html
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.auth import (
    AccessToken,
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


@dataclass
class PendingAuthorization:
    """Stored authorization context awaiting explicit user consent."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    scopes: List[str]
    created_at: float


class LocalOAuthProvider(OAuthProvider):
    """
    OAuth provider with password-protected consent and JSON-backed client storage.

    Clients are persisted to disk so that registrations survive restarts, while
    issued tokens live in memory. Authorization requests require the operator to
    approve access via a consent form that is secured with a shared password.
    """

    def __init__(
        self,
        *,
        base_url: AnyHttpUrl | str,
        issuer_url: AnyHttpUrl | str | None = None,
        service_documentation_url: AnyHttpUrl | str | None = None,
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
        required_scopes: List[str] | None = None,
        consent_password: str,
        client_store_path: Path,
        pending_ttl_seconds: int = 600,
        auth_code_ttl_seconds: int = 600,
        access_token_ttl_seconds: int = 3600,
        refresh_token_ttl_seconds: Optional[int] = 7 * 24 * 3600,
    ) -> None:
        super().__init__(
            base_url=base_url,
            issuer_url=issuer_url,
            service_documentation_url=service_documentation_url,
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            required_scopes=required_scopes,
        )
        if not consent_password:
            raise ValueError("consent_password must be provided for OAuth consent flow")
        self._consent_password = consent_password
        self._client_store_path = client_store_path
        self._client_store_path.parent.mkdir(parents=True, exist_ok=True)

        self._pending_ttl = max(60, pending_ttl_seconds)
        self._auth_code_ttl = max(60, auth_code_ttl_seconds)
        self._access_token_ttl = max(300, access_token_ttl_seconds)
        self._refresh_token_ttl = refresh_token_ttl_seconds

        self._clients: Dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: Dict[str, AuthorizationCode] = {}
        self._access_tokens: Dict[str, AccessToken] = {}
        self._refresh_tokens: Dict[str, RefreshToken] = {}
        self._pending: Dict[str, PendingAuthorization] = {}

        self._lock = asyncio.Lock()
        self._load_clients_from_disk()

    # ------------------------------------------------------------------ Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            self._clients[client_info.client_id] = client_info
            self._save_clients_to_disk()

    # ---------------------------------------------------------------- Authorization

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        scopes = self._resolve_scopes(client, params)
        pending_id = secrets.token_urlsafe(32)
        self._pending[pending_id] = PendingAuthorization(
            client=client,
            params=params,
            scopes=scopes,
            created_at=time.time(),
        )
        return f"{str(self.base_url).rstrip('/')}/oauth/consent?tx={pending_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._auth_codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if authorization_code.code not in self._auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")

        self._auth_codes.pop(authorization_code.code, None)

        access_token_value = secrets.token_urlsafe(48)
        refresh_token_value = secrets.token_urlsafe(48)

        access_expires_at = int(time.time() + self._access_token_ttl)
        refresh_expires_at = (
            int(time.time() + self._refresh_token_ttl)
            if self._refresh_token_ttl is not None
            else None
        )

        access_token = AccessToken(
            token=access_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=access_expires_at,
            resource=authorization_code.resource,
        )
        refresh_token = RefreshToken(
            token=refresh_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=refresh_expires_at,
        )

        self._access_tokens[access_token_value] = access_token
        self._refresh_tokens[refresh_token_value] = refresh_token

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=self._access_token_ttl,
            refresh_token=refresh_token_value,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        if token is None:
            return None
        if token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: List[str],
    ) -> OAuthToken:
        original_scopes = set(refresh_token.scopes)
        requested_scopes = set(scopes)
        if not requested_scopes.issubset(original_scopes):
            raise TokenError("invalid_scope", "Requested scopes exceed the scope granted by the refresh token.")

        self._refresh_tokens.pop(refresh_token.token, None)

        new_access_value = secrets.token_urlsafe(48)
        new_refresh_value = secrets.token_urlsafe(48)
        access_expires_at = int(time.time() + self._access_token_ttl)
        refresh_expires_at = (
            int(time.time() + self._refresh_token_ttl)
            if self._refresh_token_ttl is not None
            else None
        )

        self._access_tokens[new_access_value] = AccessToken(
            token=new_access_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=access_expires_at,
        )
        self._refresh_tokens[new_refresh_value] = RefreshToken(
            token=new_refresh_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=refresh_expires_at,
        )

        return OAuthToken(
            access_token=new_access_value,
            token_type="Bearer",
            expires_in=self._access_token_ttl,
            refresh_token=new_refresh_value,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._access_tokens.get(token)
        if data is None:
            return None
        if data.expires_at is not None and data.expires_at < time.time():
            self._access_tokens.pop(token, None)
            return None
        return data

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)

    # ----------------------------------------------------------------- Consent UI

    async def _handle_consent(self, request: Request) -> Response:
        tx = request.query_params.get("tx")
        form = None
        if request.method == "POST" and not tx:
            form = await request.form()
            tx = form.get("tx")
        if not tx:
            return PlainTextResponse("Missing transaction id", status_code=400)

        pending = self._pending.get(tx)
        if pending is None or self._is_pending_expired(pending):
            self._pending.pop(tx, None)
            return PlainTextResponse("Authorization request has expired. Restart the OAuth flow.", status_code=400)

        if request.method == "GET":
            return self._render_consent_page(tx, pending, error=None)

        if form is None:
            form = await request.form()
        password = form.get("password", "")
        action = form.get("action", "approve")
        if password != self._consent_password:
            return self._render_consent_page(tx, pending, error="Incorrect authorization password.")

        params = pending.params
        client = pending.client
        redirect_uri = str(params.redirect_uri)

        self._pending.pop(tx, None)

        if action == "deny":
            redirect = construct_redirect_uri(
                redirect_uri,
                error="access_denied",
                error_description="The resource owner denied the request.",
                state=params.state,
            )
            return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

        if action != "approve":
            return self._render_consent_page(tx, pending, error="Unsupported action.")

        code_value = secrets.token_urlsafe(32)
        self._auth_codes[code_value] = AuthorizationCode(
            code=code_value,
            client_id=client.client_id,
            scopes=pending.scopes,
            expires_at=time.time() + self._auth_code_ttl,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        redirect = construct_redirect_uri(
            redirect_uri,
            code=code_value,
            state=params.state,
        )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    def _render_consent_page(
        self,
        tx: str,
        pending: PendingAuthorization,
        error: Optional[str],
    ) -> HTMLResponse:
        client = pending.client
        params = pending.params
        scopes = pending.scopes or ["(none)"]

        def esc(value: str | None) -> str:
            return html.escape(value) if value is not None else ""

        error_html = (
            f'<div class="error">{esc(error)}</div>' if error else ""
        )

        content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Authorize {esc(client.client_name or client.client_id)}</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      background: #f5f5f5;
      color: #1f2933;
      margin: 0;
      padding: 0;
    }}
    .container {{
      max-width: 480px;
      margin: 2.5rem auto;
      background: #fff;
      padding: 2rem 2.25rem;
      border-radius: 16px;
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.15);
    }}
    h1 {{
      font-size: 1.5rem;
      margin-bottom: 1rem;
    }}
    dl {{
      margin: 1.5rem 0;
      padding: 0;
    }}
    dt {{
      font-weight: 600;
    }}
    dd {{
      margin: 0.25rem 0 1rem 0;
    }}
    ul {{
      margin: 0.25rem 0 0.75rem 1.25rem;
    }}
    input[type="password"] {{
      width: 100%;
      padding: 0.65rem 0.75rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      font-size: 1rem;
    }}
    .buttons {{
      display: flex;
      gap: 0.75rem;
      margin-top: 1.5rem;
    }}
    button {{
      flex: 1;
      padding: 0.85rem 1rem;
      font-size: 1rem;
      border: none;
      border-radius: 10px;
      cursor: pointer;
    }}
    button.approve {{
      background: #2563eb;
      color: #fff;
    }}
    button.deny {{
      background: #e2e8f0;
      color: #1f2933;
    }}
    .error {{
      background: #fee2e2;
      color: #991b1b;
      border-radius: 8px;
      padding: 0.75rem;
      margin-bottom: 1rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Authorize {esc(client.client_name or "OAuth Client")}</h1>
    <p>{esc(client.client_name or client.client_id)} is requesting access to the iCloud MCP server.</p>
    <dl>
      <dt>Client ID</dt>
      <dd><code>{esc(client.client_id)}</code></dd>
      <dt>Redirect URI</dt>
      <dd>{esc(str(params.redirect_uri))}</dd>
      <dt>Requested Scopes</dt>
      <dd>
        <ul>
          {''.join(f'<li>{esc(scope)}</li>' for scope in scopes)}
        </ul>
      </dd>
    </dl>
    {error_html}
    <form method="post">
      <input type="hidden" name="tx" value="{esc(tx)}" />
      <label for="password">Approval password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required />
      <div class="buttons">
        <button class="approve" name="action" value="approve" type="submit">Allow</button>
        <button class="deny" name="action" value="deny" type="submit">Deny</button>
      </div>
    </form>
  </div>
</body>
</html>
"""
        return HTMLResponse(content, status_code=200, headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- Utilities

    def _resolve_scopes(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> List[str]:
        if params.scopes is not None:
            scopes = list(params.scopes)
        elif client.scope:
            scopes = client.scope.split()
        elif (
            self.client_registration_options
            and self.client_registration_options.default_scopes
        ):
            scopes = list(self.client_registration_options.default_scopes)
        else:
            scopes = []

        valid_scopes = set(
            self.client_registration_options.valid_scopes
            if self.client_registration_options
            and self.client_registration_options.valid_scopes
            else []
        )
        if valid_scopes:
            scopes = [scope for scope in scopes if scope in valid_scopes]

        for required in self.required_scopes or []:
            if required not in scopes:
                scopes.append(required)

        return scopes

    def _is_pending_expired(self, pending: PendingAuthorization) -> bool:
        return (time.time() - pending.created_at) > self._pending_ttl

    def _load_clients_from_disk(self) -> None:
        if not self._client_store_path.exists():
            return
        try:
            data = json.loads(self._client_store_path.read_text())
        except Exception:
            return
        for row in data:
            try:
                client = OAuthClientInformationFull.model_validate(row)
            except Exception:
                continue
            self._clients[client.client_id] = client

    def _save_clients_to_disk(self) -> None:
        payload = [client.model_dump(mode="json") for client in self._clients.values()]
        self._client_store_path.write_text(json.dumps(payload, indent=2))

    def get_routes(self, mcp_path: str | None = None) -> List[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(
            Route(
                "/oauth/consent",
                endpoint=self._handle_consent,
                methods=["GET", "POST"],
            )
        )
        return routes
