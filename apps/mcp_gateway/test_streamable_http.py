"""The streamable HTTP adapter against a real server on a real socket.

A small MCP server built on the standard library -- enough of the protocol to
exercise what the adapter must get right: the handshake, sessions, answers
sent as JSON or as an event stream, pagination, errors, and credentials.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.test import SimpleTestCase

from integrations.base.config import AdapterConfig
from integrations.base.mcp import MCPProtocolError, MCPTransportError
from integrations.mcp.streamable_http import StreamableHTTPMCPAdapter, forget_sessions


def _tools(count: int) -> list[dict]:
    return [
        {"name": f"tool_{i}", "description": f"Tool {i}", "inputSchema": {"type": "object"}}
        for i in range(count)
    ]


class FakeMCPServer:
    """Streamable HTTP, with the behaviours a test needs to switch on."""

    def __init__(self, *, stream=False, session=True, page_size=None, tools=None, hold_open=0):
        self.stream = stream
        self.session = session
        self.page_size = page_size
        self.tools = tools or _tools(3)
        self.hold_open = hold_open
        self.sessions: set[str] = set()
        #: (method, lower-cased headers) for every request received.
        self.requests: list[tuple[str, dict]] = []

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def _page(self, params: dict) -> dict:
        if not self.page_size:
            return {"tools": self.tools}
        start = int(params.get("cursor") or 0)
        result = {"tools": self.tools[start:start + self.page_size]}
        if start + self.page_size < len(self.tools):
            result["nextCursor"] = str(start + self.page_size)
        return result

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                method = message.get("method")
                fake.requests.append((method, {k.lower(): v for k, v in self.headers.items()}))

                if fake.session and method != "initialize":
                    if self.headers.get("Mcp-Session-Id") not in fake.sessions:
                        self.send_response(404)
                        self.end_headers()
                        return
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return

                headers = {}
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0.1"},
                    }
                    if fake.session:
                        session_id = f"s{len(fake.sessions) + 1}"
                        fake.sessions.add(session_id)
                        headers["Mcp-Session-Id"] = session_id
                elif method == "tools/list":
                    result = fake._page(message.get("params") or {})
                elif method == "tools/call" and message["params"]["name"] == "explode":
                    self._reply({"jsonrpc": "2.0", "id": message["id"],
                                 "error": {"code": -32602, "message": "Unknown tool: explode"}}, headers)
                    return
                elif method == "tools/call":
                    params = message["params"]
                    text = f"called {params['name']} with {json.dumps(params.get('arguments'))}"
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                else:
                    self._reply({"jsonrpc": "2.0", "id": message["id"],
                                 "error": {"code": -32601, "message": "Method not found"}}, headers)
                    return
                self._reply({"jsonrpc": "2.0", "id": message["id"], "result": result}, headers)

            def _reply(self, payload, headers):
                if not fake.stream:
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # An event stream: a keep-alive comment and a progress
                # notification before the answer, as real servers send.
                progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {"progress": 1}}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.write(f"event: message\ndata: {json.dumps(progress)}\n\n".encode())
                self.wfile.write(f"event: message\ndata: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                if fake.hold_open:
                    time.sleep(fake.hold_open)

        return Handler


class StreamableHTTPAdapterTests(SimpleTestCase):
    def setUp(self):
        forget_sessions()

    def adapter(self, url, *, credential_ref="", **options):
        return StreamableHTTPMCPAdapter(
            AdapterConfig(
                key="streamable_http", name="Fake", endpoint=url,
                credential_ref=credential_ref, timeout=5, options=options,
            )
        )

    def test_the_handshake_reports_the_server_and_is_acknowledged(self):
        with FakeMCPServer() as server:
            info = self.adapter(server.url).initialize()

        self.assertEqual(info.name, "fake")
        self.assertEqual(info.protocol_version, "2025-06-18")
        self.assertEqual(server.methods(), ["initialize", "notifications/initialized"])

    def test_the_session_and_version_ride_on_later_requests(self):
        with FakeMCPServer() as server:
            self.adapter(server.url).call_tool("tool_0", {"a": 1})

        _, headers = server.requests[-1]
        self.assertEqual(headers.get("mcp-session-id"), "s1")
        self.assertEqual(headers.get("mcp-protocol-version"), "2025-06-18")

    def test_a_session_is_reused_rather_than_renegotiated_per_call(self):
        with FakeMCPServer() as server:
            adapter = self.adapter(server.url)
            adapter.call_tool("tool_0", {})
            adapter.call_tool("tool_1", {})

        self.assertEqual(server.methods().count("initialize"), 1)

    def test_a_forgotten_session_is_re_established_once(self):
        with FakeMCPServer() as server:
            adapter = self.adapter(server.url)
            adapter.call_tool("tool_0", {})
            server.sessions.clear()  # the server restarts and forgets us
            result = adapter.call_tool("tool_1", {})

        self.assertIn("called tool_1", result.text_content)
        self.assertEqual(server.methods().count("initialize"), 2)

    def test_a_stateless_server_gets_no_session_header(self):
        with FakeMCPServer(session=False) as server:
            self.adapter(server.url).call_tool("tool_0", {})

        _, headers = server.requests[-1]
        self.assertNotIn("mcp-session-id", headers)

    def test_an_answer_on_an_event_stream_is_found_among_other_events(self):
        with FakeMCPServer(stream=True) as server:
            result = self.adapter(server.url).call_tool("tool_0", {"q": "x"})

        self.assertEqual(result.text_content, 'called tool_0 with {"q": "x"}')

    def test_a_stream_left_open_does_not_hold_the_caller(self):
        """Reading stops at the answer, not at the end of the stream."""
        with FakeMCPServer(stream=True, hold_open=3) as server:
            adapter = self.adapter(server.url)
            adapter.initialize()
            started = time.monotonic()
            adapter.call_tool("tool_0", {})
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2)

    def test_tools_are_collected_across_pages(self):
        with FakeMCPServer(page_size=2, tools=_tools(5)) as server:
            tools = self.adapter(server.url).list_tools()

        self.assertEqual([tool.name for tool in tools], [f"tool_{i}" for i in range(5)])

    def test_a_json_rpc_error_is_a_protocol_error_with_its_code(self):
        with FakeMCPServer() as server:
            with self.assertRaises(MCPProtocolError) as raised:
                self.adapter(server.url).call_tool("explode", {})

        self.assertEqual(raised.exception.code, -32602)

    def test_an_unreachable_server_is_a_transport_error(self):
        with FakeMCPServer() as server:
            url = server.url
        with self.assertRaises(MCPTransportError):
            self.adapter(url).initialize()

    def test_a_bearer_credential_is_injected_from_the_environment(self):
        with patch.dict(os.environ, {"FAKE_MCP_TOKEN": "s3cret"}), FakeMCPServer() as server:
            self.adapter(server.url, credential_ref="FAKE_MCP_TOKEN", auth_method="bearer").initialize()

        _, headers = server.requests[0]
        self.assertEqual(headers.get("authorization"), "Bearer s3cret")

    def test_an_api_key_header_credential_uses_the_configured_header(self):
        with patch.dict(os.environ, {"FAKE_MCP_TOKEN": "s3cret"}), FakeMCPServer() as server:
            self.adapter(
                server.url, credential_ref="FAKE_MCP_TOKEN",
                auth_method="header", auth_header="X-Upstream-Key",
            ).initialize()

        _, headers = server.requests[0]
        self.assertEqual(headers.get("x-upstream-key"), "s3cret")
        self.assertNotIn("authorization", headers)
