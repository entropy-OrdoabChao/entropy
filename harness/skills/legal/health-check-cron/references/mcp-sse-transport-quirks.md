# Beezle MCP Server — SSE Transport Quirks

## Problem: Raw HTTP Polling Returns 404

The Beezle MCP server listens on port **8200** and accepts connections, but it implements the **MCP SSE transport protocol** — not a REST API. This means:

- `curl http://localhost:8200/` → **404 Not Found** (uvicorn serves only the `/mcp/events` SSE endpoint)
- `curl http://localhost:8200/mcp/events` → **404 Not Found** (requires proper SSE headers and initialization handshake)
- `curl -X POST http://localhost:8200/mcp` with SSE headers → **-32600 Not Acceptable** (wrong method/path combination)

All of these are **expected behavior** — the server is not broken. It expects the MCP client framework to manage the SSE lifecycle (initialization → subscribe → RPC calls).

## Correct Diagnostic Method

Use the Hermes CLI tool:

```bash
hermes mcp test beezle
```

This outputs:
- `✓ Connected (<ms>)` — SSE handshake succeeded
- `✓ Tools discovered: N` — all 130 tools registered
- Full tool listing for manual inspection

Exit code 0 = operational.

## Why This Matters

During health checks or debugging, developers often assume a listening TCP port means "API is reachable." With the Beezle MCP server, the port IS open and listening (confirmed via `ss -tlnp`), but **direct HTTP calls will always 404**. The only valid diagnostic is through the Hermes MCP client (`hermes mcp test`) or a proper MCP client library.

## Related Configuration

In `~/.hermes/config.yaml`:
```yaml
mcp_servers:
  beezle:
    url: http://127.0.0.1:8200/mcp
    enabled: true
```

The server process:
```
/home/{{USER_FIRST}}/.hermes/hermes-agent/venv/bin/python3 {{ENTROPY_HOME}}/beezle_mcp_server.py --transport http --host 0.0.0.0 --port 8200
```

PID 159, running since 2026-07-15.
