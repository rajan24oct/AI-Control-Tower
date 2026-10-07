"""A2A over JSON-RPC 2.0 and HTTP (A2A 0.3).

The agent's card is read from where it publishes it -- the record's
`card_url`, or the well-known path at the endpoint's origin. Messages go to
the endpoint on the registry record, never to the `url` the card names: a card
that starts pointing somewhere else must not redirect the gateway's traffic on
its own say-so.

Only the request-and-answer methods are used: `message/send`, `tasks/get` and
`tasks/cancel`. The gateway asks for a blocking answer and records whatever
comes back; a task still working is followed with `tasks/get`.

Stdlib only, like the MCP transport: the adapter layer adds no HTTP dependency.
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlsplit

from integrations.base.a2a import (
    WELL_KNOWN_CARD_PATHS,
    A2ACardInfo,
    A2AProtocolError,
    A2ATaskResult,
    A2ATransportError,
    BaseA2AAdapter,
)
from integrations.base.exceptions import AdapterConfigurationError
from integrations.registry import register_a2a

_ids = itertools.count(1)


class _NotFound(A2ATransportError):
    """Nothing at that URL: try the next place a card may be."""


@register_a2a("jsonrpc")
class JSONRPCA2AAdapter(BaseA2AAdapter):
    adapter_key = "jsonrpc"
    display_name = "A2A JSON-RPC over HTTP"

    # -- Contract -----------------------------------------------------------

    def get_card(self) -> A2ACardInfo:
        tried = []
        for url in self._card_urls():
            tried.append(url)
            try:
                return A2ACardInfo.from_wire(self._get(url))
            except _NotFound:
                continue
        raise A2ATransportError(f"No Agent Card at {' or '.join(tried)}.")

    def send_message(self, message, *, metadata=None, configuration=None) -> A2ATaskResult:
        params: dict[str, Any] = {"message": message}
        if configuration:
            params["configuration"] = configuration
        if metadata:
            params["metadata"] = metadata
        return A2ATaskResult.from_wire(self._rpc("message/send", params))

    def get_task(self, task_id: str) -> A2ATaskResult:
        return A2ATaskResult.from_wire(self._rpc("tasks/get", {"id": task_id}))

    def cancel_task(self, task_id: str) -> A2ATaskResult:
        return A2ATaskResult.from_wire(self._rpc("tasks/cancel", {"id": task_id}))

    # -- Wire ---------------------------------------------------------------

    def _endpoint(self) -> str:
        if not self.config.endpoint:
            raise AdapterConfigurationError(f"A2A agent '{self.config.name}' has no endpoint.")
        return self.config.endpoint

    def _card_urls(self) -> list[str]:
        explicit = self.config.option("card_url")
        if explicit:
            return [explicit]
        parts = urlsplit(self._endpoint())
        origin = f"{parts.scheme}://{parts.netloc}/"
        return [urljoin(origin, path) for path in WELL_KNOWN_CARD_PATHS]

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # Static headers first, so none of them can shadow the credential.
        headers.update(self.config.option("headers") or {})
        method = self.config.option("auth_method", "none")
        if method != "none" and self.config.credential_ref:
            secret = self.config.resolve_credential()
            if method == "header":
                headers[self.config.option("auth_header") or "X-API-Key"] = secret
            else:
                headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _open(self, request: urllib.request.Request):
        url = request.full_url
        try:
            return urllib.request.urlopen(request, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _NotFound(f"{url} returned HTTP 404.") from exc
            error = self._jsonrpc_error(exc)
            if error is not None and exc.code < 500:
                # The agent answered with a refusal in the body: it is up.
                raise error from exc
            raise A2ATransportError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # refused connections, DNS, TLS, timeouts
            raise A2ATransportError(f"Request to {url} failed: {exc}") from exc

    @staticmethod
    def _jsonrpc_error(exc: urllib.error.HTTPError) -> A2AProtocolError | None:
        try:
            body = json.loads(exc.read().decode("utf-8") or "null")
        except Exception:
            return None
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return None
        return A2AProtocolError(int(error.get("code", -32603)), str(error.get("message", "")), error.get("data"))

    def _read(self, response, url: str):
        try:
            with response:
                return json.loads(response.read().decode("utf-8") or "null")
        except ValueError as exc:
            raise A2ATransportError(f"{url} returned a body that is not JSON.") from exc
        except OSError as exc:  # the socket timed out or dropped mid-read
            raise A2ATransportError(f"Reading the response from {url} failed: {exc}") from exc

    def _get(self, url: str):
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        return self._read(self._open(request), url)

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self._endpoint()
        message_id = next(_ids)
        request = urllib.request.Request(
            url,
            data=json.dumps({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            reply = self._read(self._open(request), url)
        except _NotFound as exc:
            raise A2ATransportError(str(exc)) from exc
        if not isinstance(reply, dict):
            raise A2ATransportError(f"{url} sent no JSON-RPC response.")
        error = reply.get("error")
        if error is not None:
            error = error if isinstance(error, dict) else {"message": str(error)}
            try:
                code = int(error.get("code", -32603))
            except (TypeError, ValueError):
                code = -32603
            raise A2AProtocolError(code, str(error.get("message", "Unknown error")), error.get("data"))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise A2ATransportError("The agent's response carried no result object.")
        return result
