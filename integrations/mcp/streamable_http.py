"""Streamable HTTP MCP client (MCP 2025-03-26 and later).

One endpoint, JSON-RPC 2.0 over POST. A server may answer a request with a
plain JSON body or with a short server-sent-event stream that carries the
response; both are handled, because which one a server picks is its business.

Stdlib only, like the generic REST provider: the adapter layer adds no HTTP
dependency.

Sessions are cached per endpoint and credential for a few minutes. A full
handshake before every tool call would add two round trips to every call an
agent makes. A session the server has forgotten (it answers 404) is
re-established once and the request retried.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from integrations.base.exceptions import AdapterConfigurationError
from integrations.base.mcp import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    BaseMCPAdapter,
    MCPProtocolError,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
    MCPTransportError,
)
from integrations.registry import register_mcp

CLIENT_INFO = {
    "name": "ai-control-tower",
    "title": "AI Control Tower MCP Gateway",
    "version": "1.0.0",
}
SESSION_TTL_SECONDS = 300
#: A server that keeps handing out cursors is not going to stop on its own.
MAX_LIST_PAGES = 20

_ids = itertools.count(1)
_sessions: dict[tuple, "_Session"] = {}
_sessions_lock = threading.Lock()


@dataclass
class _Session:
    session_id: str | None
    protocol_version: str
    info: MCPServerInfo
    expires_at: float


class _SessionExpired(MCPTransportError):
    """The server no longer recognises our session id."""


def forget_sessions() -> None:
    """Drop every cached session. Intended for tests."""
    with _sessions_lock:
        _sessions.clear()


@register_mcp("streamable_http")
class StreamableHTTPMCPAdapter(BaseMCPAdapter):
    adapter_key = "streamable_http"
    display_name = "Streamable HTTP"

    # -- Contract -----------------------------------------------------------

    def initialize(self) -> MCPServerInfo:
        # Always a fresh handshake: discovery and health checks call this, and
        # a cached session would answer for a server that has since gone.
        return self._session(fresh=True).info

    def list_tools(self) -> list[MCPToolDefinition]:
        tools: list[MCPToolDefinition] = []
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            for item in result.get("tools") or []:
                if isinstance(item, dict) and item.get("name"):
                    tools.append(MCPToolDefinition.from_wire(item))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return MCPToolResult.from_wire(result)

    # -- Sessions -----------------------------------------------------------

    def _key(self) -> tuple:
        return (
            self.config.endpoint,
            self.config.credential_ref,
            self.config.option("auth_method", ""),
            self.config.option("auth_header", ""),
        )

    def _session(self, *, fresh: bool = False) -> _Session:
        key = self._key()
        if not fresh:
            with _sessions_lock:
                cached = _sessions.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached

        session = self._handshake()
        with _sessions_lock:
            _sessions[key] = session
        return session

    def _forget(self) -> None:
        with _sessions_lock:
            _sessions.pop(self._key(), None)

    def _handshake(self) -> _Session:
        reply, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(_ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
            session=None,
        )
        result = self._result_of(reply)
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPProtocolError(
                -32602, f"Server offered unsupported protocol version '{version}'."
            )

        session = _Session(
            session_id=headers.get("mcp-session-id") or None,
            protocol_version=version,
            info=MCPServerInfo.from_initialize(result),
            expires_at=time.monotonic() + SESSION_TTL_SECONDS,
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
        return session

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
        try:
            reply, _ = self._post(message, session=self._session())
        except _SessionExpired:
            self._forget()
            reply, _ = self._post(message, session=self._session(fresh=True))
        return self._result_of(reply)

    # -- Wire ---------------------------------------------------------------

    def _url(self) -> str:
        if not self.config.endpoint:
            raise AdapterConfigurationError(f"MCP server '{self.config.name}' has no endpoint.")
        return self.config.endpoint

    def _headers(self, session: _Session | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # Static headers first, so none of them can shadow the credential.
        headers.update(self.config.option("headers") or {})

        method = self.config.option("auth_method", "none")
        if method != "none" and self.config.credential_ref:
            secret = self.config.resolve_credential()
            if method == "header":
                headers[self.config.option("auth_header") or "X-API-Key"] = secret
            else:
                headers["Authorization"] = f"Bearer {secret}"

        if session is not None:
            headers["MCP-Protocol-Version"] = session.protocol_version
            if session.session_id:
                headers["Mcp-Session-Id"] = session.session_id
        return headers

    def _post(self, message: dict, *, session: _Session | None) -> tuple[dict | None, dict]:
        url = self._url()
        request = urllib.request.Request(
            url,
            data=json.dumps(message).encode("utf-8"),
            headers=self._headers(session),
            method="POST",
        )
        deadline = time.monotonic() + self.config.timeout

        try:
            response = urllib.request.urlopen(request, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and session is not None and session.session_id:
                raise _SessionExpired(f"{url} no longer recognises session.") from exc
            raise MCPTransportError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # refused connections, DNS, TLS, timeouts
            raise MCPTransportError(f"Request to {url} failed: {exc}") from exc

        try:
            with response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                if "id" not in message:
                    # A notification: the server owes us nothing but a 202.
                    return None, headers
                content_type = headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type == "text/event-stream":
                    return self._read_stream(response, message["id"], deadline), headers
                return self._match(json.loads(response.read().decode("utf-8") or "null"), message["id"]), headers
        except MCPTransportError:
            raise
        except ValueError as exc:
            raise MCPTransportError(f"{url} returned a body that is not JSON.") from exc
        except OSError as exc:  # the socket timed out or dropped mid-read
            raise MCPTransportError(f"Reading the response from {url} failed: {exc}") from exc

    def _read_stream(self, response, wanted_id, deadline: float) -> dict | None:
        """Read server-sent events until the one answering our request arrives.

        The server may send progress notifications or log messages first; they
        are skipped. Reading stops at the answer rather than at end of stream,
        so a server that keeps the stream open cannot hold the caller.
        """
        data: list[str] = []
        while True:
            if time.monotonic() > deadline:
                raise MCPTransportError("Timed out waiting for the response on the event stream.")
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                reply = self._event(data, wanted_id)
                data = []
                if reply is not None:
                    return reply
                continue
            if line.startswith(":"):
                continue  # a comment, typically a keep-alive
            name, _, value = line.partition(":")
            if name == "data":
                data.append(value[1:] if value.startswith(" ") else value)

        # The last event may arrive without its closing blank line.
        reply = self._event(data, wanted_id)
        if reply is not None:
            return reply
        raise MCPTransportError("The event stream closed without a response.")

    @classmethod
    def _event(cls, data: list[str], wanted_id) -> dict | None:
        if not data:
            return None
        try:
            payload = json.loads("\n".join(data))
        except ValueError:
            return None
        return cls._match(payload, wanted_id)

    @staticmethod
    def _match(payload, wanted_id) -> dict | None:
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("id") == wanted_id
                and ("result" in candidate or "error" in candidate)
            ):
                return candidate
        return None

    @staticmethod
    def _result_of(reply: dict | None) -> dict[str, Any]:
        if reply is None:
            raise MCPTransportError("The server sent no response.")

        error = reply.get("error")
        if error is not None:
            error = error if isinstance(error, dict) else {"message": str(error)}
            try:
                code = int(error.get("code", -32603))
            except (TypeError, ValueError):
                code = -32603
            raise MCPProtocolError(code, str(error.get("message", "Unknown error")), error.get("data"))

        result = reply.get("result")
        if not isinstance(result, dict):
            raise MCPTransportError("The server's response carried no result object.")
        return result
