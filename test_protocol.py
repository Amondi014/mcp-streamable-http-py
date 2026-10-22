"""Conformance test suite for MCP 2025-11-25 Streamable HTTP Server.

Tests run against a real running server over HTTP without external test runners
or dependencies (using only python's unittest and urllib).
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from server import McpServer, PROTOCOL_VERSION


class TestMcpProtocolConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server on ephemeral port (8991)
        cls.port = 8991
        cls.base_url = f"http://127.0.0.1:{cls.port}/mcp"
        cls.server = McpServer("127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)  # Brief warm up

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, payload, headers=None):
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                body = resp.read()
                return status, resp_headers, body
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_01_initialize_handshake(self):
        """Test initialize returns 200, session ID, protocol version, and server capabilities."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        status, headers, body = self._post(payload)
        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        self.assertEqual(headers.get("MCP-Protocol-Version"), PROTOCOL_VERSION)

        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("id"), 1)
        result = data.get("result", {})
        self.assertEqual(result.get("protocolVersion"), PROTOCOL_VERSION)
        self.assertIn("tools", result.get("capabilities", {}))

    def test_02_notification_returns_202_no_body(self):
        """Test initialized notification returns HTTP 202 with an empty body."""
        # 1. Initialize first
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        # 2. Send notification (no 'id' field)
        notif_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        status, _, body = self._post(notif_payload, headers={"Mcp-Session-Id": session_id})
        self.assertEqual(status, 202)
        self.assertEqual(len(body), 0)

    def test_03_tools_list(self):
        """Test tools/list returns declared tools with schemas."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        tools = data.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        self.assertIn("echo", tool_names)
        self.assertIn("add_numbers", tool_names)
        self.assertIn("get_server_time", tool_names)

    def test_04_tools_call_success(self):
        """Test executing tool returns structured content and isError: False."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        status, _, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "add_numbers", "arguments": {"a": 15, "b": 27}},
            },
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        result = data.get("result", {})
        self.assertFalse(result.get("isError"))
        self.assertEqual(result["content"][0]["text"], "42.0")

    def test_05_tools_call_invalid_params_returns_isError(self):
        """Test invalid tool arguments return isError: True, not a protocol error."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        status, _, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "add_numbers", "arguments": {"a": 10}},  # missing 'b'
            },
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        result = data.get("result", {})
        self.assertTrue(result.get("isError"))
        self.assertIn("Invalid arguments", result["content"][0]["text"])

    def test_06_method_not_found(self):
        """Test calling unknown method returns standard JSON-RPC -32601."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 5, "method": "non_existent_method", "params": {}},
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32601)

    def test_07_missing_session_header(self):
        """Test post-initialize requests without Mcp-Session-Id get HTTP 400."""
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}}
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Missing Mcp-Session-Id", body)

    def test_08_unknown_session_header(self):
        """Test requests with unknown Mcp-Session-Id get HTTP 404."""
        status, _, body = self._post(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
            headers={"Mcp-Session-Id": "bad-session-token-999"},
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Unknown or expired session", body)

    def test_09_batch_requests_rejected(self):
        """Test JSON-RPC batch arrays are rejected with InvalidRequest."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        status, _, body = self._post(
            [{"jsonrpc": "2.0", "id": 8, "method": "ping"}],
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32600)
        self.assertIn("batching is not supported", data["error"]["message"])

    def test_10_delete_terminates_session(self):
        """Test DELETE /mcp destroys session; subsequent calls return 404."""
        status, headers, _ = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        session_id = headers["Mcp-Session-Id"]

        # Delete session
        req = urllib.request.Request(
            self.base_url,
            headers={"Mcp-Session-Id": session_id},
            method="DELETE",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 204)

        # Subsequent call with deleted session ID must return 404
        status, _, _ = self._post(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
