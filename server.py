"""Pure Python Model Context Protocol (MCP) Streamable HTTP Server.

Specification revision: 2025-11-25.
Zero external dependencies: uses only Python standard library.

Transport:
  POST   /mcp   Single JSON-RPC 2.0 request, notification or response
  GET    /mcp   SSE stream for server-to-client notifications
  DELETE /mcp   Terminate an existing session
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("mcp_server")
PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {
    "name": "mcp-streamable-http-py",
    "version": "1.0.0",
    "title": "Pure Python MCP Streamable HTTP Reference Server",
}

# Standard JSON-RPC 2.0 Error Codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolRegistry:
    """Registry for tools exposed by the MCP server."""

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Any],
        read_only: bool = True,
        idempotent: bool = True,
        open_world: bool = False,
    ):
        """Register a tool with its schema and handler."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "readOnly": read_only,
            "idempotent": idempotent,
            "openWorld": open_world,
        }
        self._handlers[name] = handler

    def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools.values())

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self._handlers:
            raise KeyError(f"Tool not found: {name}")
        handler = self._handlers[name]
        return handler(arguments)


class SessionManager:
    """Manages active MCP client sessions."""

    def __init__(self, ttl_seconds: int = 3600):
        self._sessions: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def create(self) -> str:
        session_id = secrets.token_hex(16)
        with self._lock:
            self._sessions[session_id] = time.time()
        return session_id

    def validate(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        with self._lock:
            if session_id not in self._sessions:
                return False
            # Refresh last activity
            self._sessions[session_id] = time.time()
            return True

    def remove(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def cleanup(self):
        now = time.time()
        with self._lock:
            expired = [sid for sid, ts in self._sessions.items() if now - ts > self._ttl]
            for sid in expired:
                del self._sessions[sid]


class McpRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler implementing MCP 2025-11-25 Streamable HTTP."""

    server_version = "mcp-streamable-http/1.0"

    def do_OPTIONS(self):
        """CORS preflight support."""
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Mcp-Session-Id, MCP-Protocol-Version, Authorization",
        )
        self.end_headers()

    def do_POST(self):
        """Process incoming MCP JSON-RPC requests."""
        if self.path != "/mcp":
            self.send_error(404, "Endpoint not found")
            return

        # Check content length
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid Content-Length")
            return

        if content_length <= 0:
            self.send_error(400, "Empty request body")
            return

        body = self.rfile.read(content_length)

        # Parse JSON
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_json_rpc_error(None, PARSE_ERROR, "Invalid JSON received")
            return

        # Reject JSON-RPC batches per modern MCP specification
        if isinstance(payload, list):
            self._send_json_rpc_error(
                None, INVALID_REQUEST, "JSON-RPC batching is not supported in MCP"
            )
            return

        if not isinstance(payload, dict):
            self._send_json_rpc_error(None, INVALID_REQUEST, "Invalid request object")
            return

        method = payload.get("method")
        msg_id = payload.get("id")
        params = payload.get("params", {})

        # Handle 'initialize'
        if method == "initialize":
            session_id = self.server.sessions.create()
            client_version = params.get("protocolVersion", PROTOCOL_VERSION)
            response_data = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": True},
                    },
                    "serverInfo": SERVER_INFO,
                },
            }
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Mcp-Session-Id", session_id)
            self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
            encoded = json.dumps(response_data).encode("utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        # For all other methods, Mcp-Session-Id is mandatory
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b"Missing Mcp-Session-Id header")
            return

        if not self.server.sessions.validate(session_id):
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(b"Unknown or expired session")
            return

        # Handle notifications (requests without an 'id')
        if msg_id is None:
            # Notifications receive HTTP 202 with no body
            self.send_response(202)
            self._send_cors_headers()
            self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
            self.end_headers()
            return

        # Dispatch methods
        try:
            if method == "tools/list":
                tools = self.server.tools.list_tools()
                result = {"tools": tools}
                self._send_json_rpc_result(msg_id, result)
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                if not tool_name:
                    self._send_json_rpc_error(msg_id, INVALID_PARAMS, "Missing tool name")
                    return
                try:
                    tool_output = self.server.tools.call_tool(tool_name, arguments)
                    result = {
                        "content": [{"type": "text", "text": str(tool_output)}],
                        "isError": False,
                    }
                except KeyError:
                    result = {
                        "content": [{"type": "text", "text": f"Tool '{tool_name}' not found"}],
                        "isError": True,
                    }
                except ValueError as ve:
                    result = {
                        "content": [{"type": "text", "text": f"Invalid arguments: {str(ve)}"}],
                        "isError": True,
                    }
                except Exception as ex:
                    result = {
                        "content": [{"type": "text", "text": f"Execution error: {str(ex)}"}],
                        "isError": True,
                    }
                self._send_json_rpc_result(msg_id, result)
            elif method == "ping":
                self._send_json_rpc_result(msg_id, {})
            else:
                self._send_json_rpc_error(
                    msg_id, METHOD_NOT_FOUND, f"Method '{method}' not found"
                )
        except Exception as e:
            LOG.exception("Internal error processing method %s", method)
            self._send_json_rpc_error(msg_id, INTERNAL_ERROR, str(e))

    def do_GET(self):
        """Handle Server-Sent Events (SSE) stream on /mcp."""
        if self.path != "/mcp":
            self.send_error(404, "Endpoint not found")
            return

        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id or not self.server.sessions.validate(session_id):
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            return

        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        self.end_headers()

        # Send initial endpoint event
        try:
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_DELETE(self):
        """Terminate session on DELETE /mcp."""
        if self.path != "/mcp":
            self.send_error(404, "Endpoint not found")
            return

        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id or not self.server.sessions.validate(session_id):
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()
            return

        self.server.sessions.remove(session_id)
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _send_json_rpc_result(self, msg_id: Any, result: Any):
        payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json_rpc_error(self, msg_id: Any, code: int, message: str):
        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class McpServer(ThreadingHTTPServer):
    """Threading HTTP server with attached MCP session and tool registries."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000):
        super().__init__((host, port), McpRequestHandler)
        self.sessions = SessionManager()
        self.tools = ToolRegistry()
        self._register_default_tools()

    def _register_default_tools(self):
        # Sample Tool 1: Echo
        self.tools.register(
            name="echo",
            description="Returns the provided text unchanged.",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            handler=lambda args: args.get("message", ""),
            read_only=True,
            idempotent=True,
        )

        # Sample Tool 2: Add Numbers
        def _add(args):
            a = args.get("a")
            b = args.get("b")
            if a is None or b is None:
                raise ValueError("Both 'a' and 'b' parameters are required.")
            return float(a) + float(b)

        self.tools.register(
            name="add_numbers",
            description="Adds two numbers and returns the sum.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "First number"},
                    "b": {"type": "number", "description": "Second number"},
                },
                "required": ["a", "b"],
            },
            handler=_add,
            read_only=True,
            idempotent=True,
        )

        # Sample Tool 3: Server Info
        self.tools.register(
            name="get_server_time",
            description="Returns the current server UTC timestamp.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
            read_only=True,
            idempotent=False,
        )


def run_server(host: str = "127.0.0.1", port: int = 8000):
    server = McpServer(host, port)
    print(f"MCP Streamable HTTP Server (spec {PROTOCOL_VERSION}) running at http://{host}:{port}/mcp")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pure Python MCP Streamable HTTP Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    args = parser.parse_args()
    run_server(args.host, args.port)
