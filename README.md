# mcp-streamable-http-py

A zero-dependency, pure Python standard-library implementation of the **Model Context Protocol (MCP)** specification (revision `2025-11-25`), featuring the **Streamable HTTP** transport.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.8+](https://img.shields.io/badge/Python-3.8%2B-brightgreen.svg)]()
[![Tests: Passing](https://img.shields.io/badge/Tests-10%20passing-success.svg)]()

---

## Highlights

- **Zero dependencies:** Built using Python's standard library (`http.server`, `urllib`, `json`, `threading`). No `pip install` required.
- **Spec Compliant:** Adheres strictly to the official MCP 2025-11-25 Streamable HTTP specification.
- **Session Lifecycle:** Robust session tracking (`Mcp-Session-Id`), automatic expiration, and explicit deletion (`DELETE /mcp`).
- **Streamable HTTP Routes:**
  - `POST /mcp`: Handles JSON-RPC 2.0 requests and notifications.
  - `GET /mcp`: Establishes Server-Sent Events (SSE) stream for server notifications.
  - `DELETE /mcp`: Closes and cleans up client sessions.
- **JSON-RPC Precision:** Proper error codes (`-32700`, `-32600`, `-32601`, `-32602`), rejection of deprecated JSON-RPC batching, and clean separation between transport errors and tool execution errors (`isError: true`).
- **Comprehensive Conformance Suite:** Includes 10 automated HTTP integration tests verifying protocol behavior.

---

## Quickstart

### 1. Run the Server

Clone the repository and start the server with your local Python installation:

```bash
python server.py --host 127.0.0.1 --port 8000
```

The server binds to `http://127.0.0.1:8000/mcp`.

### 2. Run Conformance Tests

Run the test suite against a live ephemeral test server:

```bash
python test_protocol.py
```

Output:
```text
Ran 10 tests in 0.86s
OK
```

---

## Registering Custom Tools

You can import `McpServer` and register custom Python functions as MCP tools with custom input schemas:

```python
from server import McpServer

# 1. Initialize server instance
server = McpServer("127.0.0.1", 8000)

# 2. Define tool handler
def calculate_vat(arguments):
    amount = arguments.get("amount")
    rate = arguments.get("rate", 0.15)
    if amount is None:
        raise ValueError("Missing 'amount' argument.")
    return amount * rate

# 3. Register tool schema
server.tools.register(
    name="calculate_vat",
    description="Calculates VAT for a given currency amount.",
    input_schema={
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "Base amount"},
            "rate": {"type": "number", "description": "VAT rate as decimal"},
        },
        "required": ["amount"],
    },
    handler=calculate_vat,
    read_only=True,
    idempotent=True,
)

# 4. Start serving
server.serve_forever()
```

---

## Protocol Details

### Handshake (`initialize`)

Clients initiate an MCP session via `POST /mcp`:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "sample-client", "version": "1.0.0"}
  }
}
```

The server responds with HTTP 200, an issued `Mcp-Session-Id` header, and server capabilities. Subsequent requests must supply the `Mcp-Session-Id` header.

### Notifications

Notifications (such as `notifications/initialized`) do not include an `id` field. The server immediately returns **HTTP 202 Accepted** with an empty body, avoiding client hangs.

---

## License

This project is licensed under the [MIT License](LICENSE).
