# SOUL.md — Hermes Agent System Configuration & Conventions

## § 0. SESSION STARTUP PROTOCOL — READ THIS BEFORE ANY ACTION

**Every session, kanban worker, cron job, and chat exchange begins by mentally consulting these in order:**

1. **`/home/{{USER_FIRST}}/.hermes/AGENTS.md`** — the operational contract (paths, prohibitions, tool-use discipline, chat-priority rule, beezle operating principles §14)
2. **`{{ENTROPY_HOME}}/obsidian_sync/00_system/lessons_learned.md`** — past failures and their fixes (READ BEFORE retrying any tool that previously failed)
3. **`{{ENTROPY_HOME}}/obsidian_sync/00_system/function_flow.md`** — which scripts touch which paths (CONSULT BEFORE editing any code or moving any file)
4. **`{{ENTROPY_HOME}}/obsidian_sync/00_system/secrets_reference.md`** — LITERAL values of auth tokens, ports, and endpoints. When a skill references a token like `beezle-dashboard-2026`, this file confirms it's the real value, not a placeholder.

You do not need to literally re-read these every turn. You DO need to mentally invoke them when:
- A tool fails — check lessons_learned.md before retrying
- You're about to edit code or move files — check function_flow.md
- You're about to call a `mcp_beezle_*` tool — check lessons_learned.md "phantom tools" section
- You're about to write a PowerShell command — check AGENTS.md § 11 (you're in bash by default)
- You're about to delete anything — check AGENTS.md § 3.1 (NEVER delete; move to .trash with .why.md)

**If you ignore this protocol, you will hallucinate tools that don't exist, repeat failed patterns, and break configs that were carefully set up. The lessons file exists precisely because previous sessions did exactly that.**

### § 0.1 — POST-ACTION VERIFICATION PROTOCOL (mandatory)

After any action that claims to create, modify, or delete state (a file, a kanban task, a skill, an Obsidian note, a configuration), do NOT declare success until you have:

1. **Run a verification command that READS BACK the changed state.** Examples:
   - Claimed to write `/path/to/file.md` → run `ls -la /path/to/file.md && head -10 /path/to/file.md`
   - Claimed to create kanban task `t_XXXX` → curl GET that exact task ID
   - Claimed to create skill `foo` → `ls ~/.hermes/skills/foo/SKILL.md && head -10 ~/.hermes/skills/foo/SKILL.md`

2. **Paste the actual output of the verification, not a summary.** "Verified ✓" is not acceptable. Show the bytes.

3. **If verification fails or returns empty, REPORT THE FAILURE.** Do not fabricate the response that should have come back. The correct format is:
   "I attempted X. Verification returned: <literal output>. The action did NOT succeed as expected."

4. **If you skip verification for any reason, explicitly say "UNVERIFIED" in your response.** Never imply something is done when you haven't read it back.

### § 0.2 — ONE ACTION PER RESPONSE (for multi-step requests)

If the user asks for multiple things in one message ("create N tasks AND link them AND make a skill"), DO NOT attempt all of them in one turn. Instead:

1. List what was asked, decomposed into atomic actions
2. Do the FIRST atomic action only
3. Verify it (per § 0.1)
4. Stop and wait for the user to say "next" or auto-continue if confidence is high

This prevents the multi-step fabrication failure mode where the model produces plausible-sounding output for steps it didn't actually execute.


**Pre-flight skill:** the `pre-flight-check` skill at `~/.hermes/skills/pre-flight-check/SKILL.md` codifies this. Workers under the kanban dispatcher should invoke it via `skill_load pre-flight-check` as their first action.

---

# SOUL.md — Hermes Agent System Configuration & Conventions

## Purpose
This file contains the canonical system configuration, path conventions, and operational guardrails for Hermes Agent in the Legal AI stack. All agents MUST follow these conventions.

---

## 1. Path Conventions

### 1.1 File System Paths
**RULE:** Always use Linux-style absolute paths under `/opt/data/`

| WRONG | CORRECT |
|-------|---------|
| `{{ENTROPY_HOME}}\obsidian_sync\cases` | `/opt/data/obsidian_sync/cases` |
| `{{ENTROPY_HOME}}/obsidian_sync/cases` | `/opt/data/obsidian_sync/cases` |
| `obsidian_sync/cases/{{CASE_SLUG}}/CASE_FACTS.md` | `/opt/data/obsidian_sync/cases/{{CASE_SLUG}}/CASE_FACTS.md` |
| `/workspace/CASE_FACTS` | (use actual path from `mcp_beezle_list_cases`) |

**WHY:** This is WSL running on Windows. Windows paths must be translated to `/opt/data/` for all file operations.

### 1.2 Skill File Paths
**RULE:** Skill reference files must be under `references/`, `scripts/`, or `templates/` subdirectories RELATIVE TO THE SKILL DIRECTORY

| WRONG | CORRECT |
|-------|---------|
| `/opt/data/skills/legal/draft-quality-auditor/references/audit.md` | `skill_path / "references" / "audit.md"` |
| `/opt/data/skills/legal/intake-analysis/references/log-format.md` | Use `write_file` to `references/` from skill root |
| `legal/pre-draft-legal-guardrail/references/checklist.md` | `references/gate-enforcement-checklist.md` |

**WHY:** `skill_view` and `skill_manage` validate paths are under allowed subdirectories.

### 1.3 Patch Strings

## 2.3 DOCKER VS NATIVE MCP (2026-05-23 - CRITICAL)

**KEY LEARNING:** Docker containers are **NOT required** for MCP tool operation.

**Configuration:**
- Beezle MCP: `http://host.docker.internal:8200/mcp`
- Model server: `http://host.docker.internal:8000/v1`
- Both point to Docker host, NOT WSL

**Operational Reality:**
- MCP tools work via **native stdio connections** even when Docker is DOWN
- Docker containers are **OPTIONAL** for basic legal workflows
- Health checks can proceed with MCP tools even if Docker is unreachable

**When to Worry About Docker:**
- Only if MCP tools are ALSO failing (native stdio breaks)
- When container-based features are explicitly needed
- When disk I/O or file operations need Docker-mounted volumes

**Health Check Pattern:**
```bash
# Check MCP Beezle (NATIVE - works even if Docker is down)
mcp_beezle_list_cases > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "  OK MCP Beezle: Operational (native stdio)"
else
    echo "  X MCP Beezle: UNREACHABLE"
fi

# Check Docker containers (OPTIONAL)
docker ps --format '{{.Names}}' > /tmp/containers.txt 2>/dev/null
if [ -s /tmp/containers.txt ]; then
    echo "  OK Docker: Responding"
else
    echo "  WARNING Docker: Not responding (MCP still works via native stdio)"
fi
```

---

**RULE:** Always use unique context strings for `patch()` operations
```python
patch(path="SOUL.md", old_string="# SOUL.md", new_string="# Updated SOUL.md")
# ERROR: Not unique — matches multiple files!
```

**CORRECT:**
```python
patch(path="SOUL.md", old_string="# SOUL.md — Hermes Agent System Configuration\n\n## Purpose", 
      new_string="# SOUL.md — Hermes Agent System Configuration (Updated 2026-05-22)\n\n## Purpose")
```

---

## 2. Tool Usage Guardrails

### 2.1 skill_manage() Best Practices

#### For skill_create:
```python
# ALWAYS validate content before calling
content = """
---
name: my-skill
description: Skill description here
---
# Body content...
"""
assert content.startswith("---"), "Must start with YAML frontmatter"
assert "name:" in content, "Must specify name"
skill_manage(action="create", name="my-skill", content=content)
```

#### For skill_patch:
```python
# Use unique strings — include surrounding context
old_string = "---\nname: draft-quality-auditor\ndescription: Post-draft quality audit cron. Reviews generated legal drafts for\n"
new_string = "---\nname: draft-quality-auditor\ndescription: Post-draft quality audit cron. Reviews generated legal drafts for compliance.\n"
patch(path="SKILL.md", old_string=old_string, new_string=new_string)
```

#### For skill_write_file:
```python
# Only write to: assets/, references/, scripts/, templates/
# Relative to the skill directory
skill_manage(
    action="write_file",
    file_path="references/audit_findings_20260522.md",  # NOT absolute path!
    file_content="...md content..."
)
```

### 2.2 MCP Tool Retry Strategy

```python
# Circuit breaker pattern
consecutive_failures = 0

def safe_mcp_tool(tool_name, *args, **kwargs):
    global consecutive_failures
    try:
        result = tool(*args, **kwargs)
        consecutive_failures = 0  # Reset on success
        return result
    except Exception as e:
        consecutive_failures += 1
        if consecutive_failures >= 3:
            logger.warning(f"Skipping {tool_name} after {consecutive_failures} failures")
            raise CircuitBreakerError(f"{tool_name} unavailable")
        raise
```

### 2.3 Four-Gates Correct Usage

```python
# CORRECT parameter names:
four_gates(
    jurisdiction_inputs={
        "court_type": "federal",
        "claim_arises_under": "federal_law",
        ...
    },
    standing_inputs={
        "injury_in_fact": "...",  # REQUIRED positional-style
        "injury_concrete_particularized": "...",
        "causation_traceable": "...",
        "redressable_by_court": "..."
    },
    cause_inputs={
        "statute_or_common_law": "...",
        "elements_identified": [...],  # NOT elements_satisfied!
        "elements_count": 3,
        "private_right_of_action": True
    },
    plausibility_inputs={
        "elements_satisfied": [...],  # This one IS correct
        "elements_total": 3,
        "facts_specific": True,
        "threadbare_recitals_only": False
    }
)
```

---

## 3. Legal Pipeline Pre-flight Checks

### 3.1 Mandatory Before Any Draft
```python
# Step 1: Verify case exists
case = mcp_beezle_get_case(case_id)
if case is None:
    raise ValueError(f"Case {case_id} not found")

# Step 2: Verify state initialized
lifecycle = mcp_beezle_lifecycle_list(case_id=case_id)
if not lifecycle:
    raise ValueError(f"No lifecycle record for {case_id}. Run pipeline_intake first.")

# Step 3: Check MCP server health
health = mcp_beezle_read_resource(uri="mcp://beezle/health")
if health.get("status") != "ok":
    logger.warning("MCP server unhealthy, may skip complex operations")
```

### 3.2 Token Budget Enforcement
```python
# Before any tool call that uses context
estimated_tokens = estimate_tokens_for_operation(context)
if estimated_tokens > 14000:  # Leave margin for 16384 window
    chunk = split_context(context, max_tokens=13000)
    process_chunk(chunk)
```

---

## 4. Error Handling Patterns

### 4.1 File Not Found Recovery
```python
def safe_read_file(path):
    """Read file with path normalization and fallback"""
    import os
    normalized = os.path.normpath(path)
    
    # Try multiple path formats
    paths_to_try = [
        normalized,
        os.path.expanduser(path),
        path.replace("C:", "/opt/data") if path.startswith("C:") else path
    ]
    
    for p in paths_to_try:
        try:
            return read_file(p)
        except FileNotFoundError:
            continue
    
    raise FileNotFoundError(f"File not found in any known location: {path}")
```

### 4.2 Search Deduplication
```python
def safe_search_files(pattern, path="."):
    """Prevent repeated identical searches"""
    recent = getattr(self, 'recent_searches', [])
    
    if pattern in recent[-5:]:
        raise Exception(f"Search '{pattern}' repeated recently. Use different pattern.")
    
    result = search_files(pattern=pattern, path=path)
    
    if len(recent) >= 5:
        recent.pop(0)
    recent.append(pattern)
    self.recent_searches = recent
    
    return result
```

---

## 5. Cron Job Configuration

### 5.1 Idle Timeout Settings
```yaml
# Recommended timeouts by job type
cron:
  - job: "draft-quality-auditor"
    timeout: 900  # 15 minutes for long audits
    heartbeat: 30  # Log every 30s
    
  - job: "dictionary-builder-heartbeat"
    timeout: 60   # 1 minute max
    retry_attempts: 3
    
  - job: "legal-email-orchestrator"
    timeout: 300  # 5 minutes
```

### 5.2 Graceful Skip for Missing Skills
```python
def safe_heartbeat(skill_name):
    """Skip gracefully if skill not found"""
    try:
        skill_view(name=skill_name)
        logger.info(f"Skill {skill_name} healthy")
    except Exception as e:
        logger.warning(f"Skipping {skill_name}: {e}")
        return False
    return True
```

---

## 6. Monitoring & Alerting

### 6.1 Health Check Endpoints
```python
# Check these regularly:
HEALTH_CHECKS = {
    "mcp_beezle": "curl http://localhost:8000/health",
    "chromadb": "curl http://localhost:8000/api/v2/heartbeat",
    "docker_containers": "docker ps --format '{{.Names}}'",
    "token_budget": "Check context before tool calls",
    "cron_jobs": "Check job status every hour"
}
```

### 6.2 Alert Thresholds
- MCP failures > 3 consecutive: Alert
- Cron job idle > 600s: Alert
- File path errors > 5 per hour: Alert
- Token budget > 80% used: Warning

---

## 7. Known Issue Workarounds

### 7.1 Context Window Limitations
**Issue:** Qwen3.5-9B-Q8_0 has 16K context, but Hermes requires 64K minimum.

**Workaround:**
1. Set in config.yaml: `model.context_length: 16384`
2. Split large contexts into chunks
3. Use summary tokens for older context

### 7.2 Windows Path Translation
**Issue:** Agents sometimes use Windows paths.

**Fix:**
- Add path canonicalization at tool boundary
- Document all accepted paths in agent prompts
- Use `os.path.normpath()` before file operations

### 7.3 Four-Gates Schema Drift
**Issue:** Tool parameters change, callers break.

**Fix:**
- Pin tool version in schema validation
- Add parameter name validation before calls
- Use enum values where available

---

## 8. Version History

| Date | Change | Author |
|------|--------|--------|
| 2026-05-22 | Added path conventions, tool guardrails, four-gates fixes | System |
| 2026-05-18 | Added skill management patterns | Agent |
| 2026-05-15 | Initial SOUL.md | System |

---

**Last Updated:** 2026-05-22  
**Version:** 1.2.0
