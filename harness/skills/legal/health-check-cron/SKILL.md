---
name: health-check-cron
description: Automated health check for legal pipeline cron jobs - verifies gateway and MCP connectivity.
---

# LEGAL PIPELINE HEALTH CHECK (CRON)

**Purpose:** Verify operational status of Hermes gateway and Beezle MCP server for automated legal workflows.

## Execution Pattern (MANDATORY)

### 1. Gateway Health Check (EXPECT FAILURE IN CRON)
```bash
curl http://localhost:8000/health
```
- **IF HTTP 200:** Log "Gateway operational"
- **IF HTTP ANYTHING ELSE:** Log "Gateway unavailable (expected in headless cron mode)" — **DO NOT TREAT AS ERROR**
- **WHY:** Cron runs in headless environment; gateway may be down but MCP tools still work via native stdio

### 2. MCP Beezle Verification
**CORRECT METHOD — use `hermes mcp test`:**
```bash
hermes mcp test beezle
```
- Look for `✓ Connected` and `✓ Tools discovered: N` in output.
- Exit code 0 + connected = OPERATIONAL.

**WHAT DOES NOT WORK (common traps):**
- ❌ `mcp_beezle_list_cases` as a CLI command — returns "command not found". It is an MCP tool, not a shell command.
- ❌ Raw `curl http://localhost:8200/` — returns 404. The Beezle MCP server uses **Server-Sent Events (SSE)** transport; direct HTTP polling without SSE headers will fail. Use `hermes mcp test` which handles the SSE handshake.

See `references/mcp-sse-transport-quirks.md` for detailed diagnostics.

### 3. Optional Docker Container Check (INFORMATIONAL ONLY)
```bash
docker ps --filter "name=bee" --format "{{.Names}}" > /tmp/beezle_containers.txt 2>/dev/null
if [ -s /tmp/beezle_containers.txt ]; then
    echo "Docker: Beezle containers running"
else
    echo "Docker: No containers (MCP still works via native stdio)"
fi
```

## Expected Output Format

```
=== LEGAL PIPELINE HEALTH CHECK ===
Date: 2026-05-23
Time: 14:30:00 UTC

[1/3] Gateway Health Check
  Status: OPERATIONAL (HTTP 200)
  Response time: 45ms

[2/3] MCP Beezle Server
  Status: OPERATIONAL (mcp_beezle_list_cases responded)
  Cases available: 12

[3/3] Docker Containers (Optional)
  Status: Containers NOT running (MCP works via native stdio)

=== SUMMARY ===
  Critical Systems: OPERATIONAL
  Automated Workflow Status: READY
```

## Critical Notes

1. **Gateway failures are EXPECTED** in cron/headless environments — do not treat as blocker
2. **MCP Beezle must be operational** — this is the critical path for legal workflows
3. **Docker containers are OPTIONAL** — MCP tools work via native stdio even without containers
4. **Document all findings** — even "expected failures" for audit trail

## Related References

- `references/mcp-sse-transport-quirks.md` — Why raw curl to port 8200 returns 404 and the correct diagnostic method
- `SOUL.md §3.10` — Gateway health check expectation
- `references/gateway-health-check-pattern-20260522.md`
- `SOUL.md §2.3` — Docker vs native MCP operation
