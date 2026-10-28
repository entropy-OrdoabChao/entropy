# obsidian-graph

**Purpose:** Walk the Obsidian vault as a citation graph. Find what links to a note, what a note links to, and trace multi-hop chains (statute → regulation → case → defendant). Propose new notes and links for human approval — never write to the live vault directly.

**Backed by:** `pipeline.obsidian_graph` (Session 20 / Phase H), exposed via Beezle MCP at `127.0.0.1:8200`.

---

## When to use this skill

Use these tools when the question involves **linkage** between notes:

- "What court orders reference Defendant X?" → `obsidian_backlinks`
- "What authorities does this case opinion cite?" → `obsidian_outgoing_links`
- "Find the chain from FCRA furnisher duties to dispositive case law" → `obsidian_graph_walk`
- "Which notes are tagged `#fcra-furnisher`?" → `obsidian_search_by_tag`
- "Find all notes where `frontmatter.type == statute`" → `obsidian_search_by_frontmatter`
- "What notes have no linkage at all?" → `obsidian_list_orphans`
- "Add a citation linking statute X to regulation Y" → `obsidian_propose_link` then `approve_obsidian_proposal`

**Do NOT use this skill for:**
- Text-similarity search over note bodies. Use `search_chunks` (ChromaDB) for that.
- Reading a single note. Use `read_obsidian_note`.
- Free-text "find related notes" queries with no specific note as input. Use `find_related` from `obsidian_bridge`.

---

## The 10 tools

### Query tools (read-only, fast — uses cached index)

| Tool | What it does |
|---|---|
| `obsidian_backlinks(note_path, folder_scope=None)` | List notes that link **TO** `note_path` |
| `obsidian_outgoing_links(note_path, folder_scope=None)` | List wikilinks **FROM** `note_path` |
| `obsidian_graph_walk(start, depth=2, edge_types=None, folder_scope=None, max_nodes=200)` | BFS walk through resolved wikilinks |
| `obsidian_search_by_tag(tag, frontmatter_only=True, folder_scope=None, limit=50)` | Find notes with a given tag |
| `obsidian_search_by_frontmatter(field, value, folder_scope=None, limit=50)` | Find notes by frontmatter field |
| `obsidian_list_orphans(folder_scope=None, limit=100)` | Notes with no incoming or outgoing links |
| `obsidian_graph_stats(folder_scope=None)` | Cached index statistics |

### Proposal tools (write to staging, NOT the live vault)

| Tool | What it does |
|---|---|
| `obsidian_propose_note(subdir, filename, content, frontmatter, links_to)` | Stage a new note for human approval |
| `obsidian_propose_link(from_note, to_note, edge_type, citation, scope)` | Stage adding a wikilink with edge metadata |
| `approve_obsidian_proposal(proposal_id)` | Apply a staged proposal to the live vault |

---

## Folder scope (defaults and overrides)

By default, queries **exclude** these noisy folders:
- `primary_sources/` (34K USC/CFR monolith files — opt-in only)
- `unmatched_emails/` (19K email feedstock — opt-in only)
- `__pycache__/`, `_workers_v18f2/`, `dev/`
- `_index/`, `_proposals/` (the tool's own cache and staging dirs)

To include them, pass `folder_scope` as an explicit list:

```python
obsidian_graph_walk(
    start="Defendants/Equifax.md",
    folder_scope=["cases", "primary_sources", "Defendants"],
)
```

When `folder_scope=None` (default), the system uses the safe exclusion list. Most queries should use the default unless you specifically need to query USC/CFR text.

---

## Edge types

Wikilinks in this vault use the convention `[[target|edge_type:pin_cite]]`. Recognized edge types:

| Edge type | Meaning |
|---|---|
| `cross_ref` | Internal same-statute reference (e.g. "see § 1681i(a)") |
| `authority` | CFR → USC enabling-statute citation |
| `defined_by` | Term imported from another section's definitions |
| `implements` | Regulation implements a statute |
| `incorporates` | By-reference cite to another regulation |
| `enforced_by` | Agency enforcement designation |
| `interpreted_by` | Guidance, ruling, or opinion interpreting a reg |
| `judicial_overlay` | Case decision applying or limiting a statute |
| `unknown` | Legacy wikilink with no edge-type alias (most existing links) |

When proposing a new link, **pick the most specific edge_type that the primary text supports** and provide a `citation` (pin cite). If you can't, use `unknown` and explain why in the note body.

---

## The violation-chain pattern

This is the canonical workflow for "find what's linked to this fact." It's three calls, in order:

### Step 1: Seed from a known node

Start with something concrete from the case ground truth:
- A statute citation: `15 USC 1681i`
- A defendant: `Defendants/Equifax.md`
- A court order: `document_notes/equifax/equifax-ecf-122-{{JUDGE}}-2025-09-19.md`

### Step 2: Walk outward, depth 2

```python
walk = obsidian_graph_walk(
    start="Defendants/Equifax.md",
    depth=2,
    max_nodes=50,
)
```

Returns: visited nodes by depth + edges traversed. Look at `by_depth["1"]` for direct neighbors and `by_depth["2"]` for next-hop relations.

### Step 3: For each interesting depth-1 hit, inspect

For each node that looks promising, call `obsidian_outgoing_links` to see what authorities it cites. Track:
- The edge type (was it `judicial_overlay`? `defined_by`?)
- The pin cite (which subsection?)
- Whether the link resolved (broken links = orphan targets that may need stub notes)

If a chain has gaps — i.e., you see two related concepts that aren't linked — use `obsidian_propose_link` to surface the connection with a citation, then `approve_obsidian_proposal` to commit it once verified.

---

## When to propose a new note

If a graph walk surfaces a concept that doesn't have a note yet (e.g. you find "TIGTA" mentioned in 5 different chains but no `obsidian_sync/agency_routes/TIGTA.md`), propose one:

```python
obsidian_propose_note(
    subdir="agency_routes",
    filename="TIGTA",
    content="""# TIGTA — Treasury Inspector General for Tax Administration

## Authority
TIGTA was established by [[Pub_L_105-206|authority:I.R.C. § 7803(d)]] (IRS Restructuring and Reform Act of 1998).

## Jurisdiction
Investigates IRS personnel misconduct under [[26_USC_7803|defined_by:7803(d)]].

## Complaint form
TIGTA Form 9024 (online portal: treasury.gov/tigta).
""",
    frontmatter={
        "type": "agency",
        "tags": ["agency", "tax", "irs", "tigta"],
        "context": "IRS misconduct complaints",
    },
    links_to=["Pub_L_105-206", "26_USC_7803"],
)
```

Then `approve_obsidian_proposal(proposal_id)` to commit it.

The propose pattern is **mandatory**. Never use `write_obsidian_note` to add notes to the legal-reasoning graph; that bypasses human review.

---

## Index freshness

The wikilink index is cached at `obsidian_sync/_index/wikilink_index_default.json` (~3MB). It rebuilds automatically when stale (>24h). A full rebuild scans ~12K notes and takes ~2.5 minutes.

If you've made many writes and want a fresh index now, the cache will rebuild on the next query past 24h, or you can force-rebuild from Python:

```python
from pipeline import obsidian_graph as og
og.build_wikilink_index(force=True)
```

---

## Known vault rot (do not worry about, but be aware)

The current vault has ~15,800 broken wikilinks, mostly the pattern `[[Cases/Equifax]]` where `Cases/` (capitalized) doesn't exist — the actual folder is `cases/` (lowercase) with subdirectories. These show up in `obsidian_outgoing_links` as `resolved_to: null` and `edge_type: unknown`. They don't break the tool; they just terminate walks early at those hops.

Don't propose links to broken targets. If you need a target that doesn't exist, propose the target note first, approve it, then propose the link.

---

## Don'ts

- **Don't query `primary_sources/` by default** — 34K USC/CFR notes will drown signal. Opt in only when the question specifically needs USC/CFR text.
- **Don't write directly to `obsidian_sync/_proposals/`** — that's the staging area; use the propose tools.
- **Don't follow `unknown` edges blindly when building a violation chain.** Look for `authority`, `defined_by`, `implements`, `judicial_overlay` — those have textual basis.
- **Don't fabricate edge_types.** If the primary text doesn't support `authority` or `implements`, use `unknown` and note it.

---

## See also

- `pipeline/obsidian_graph.py` — implementation
- `obsidian_sync_tools/obsidian_query.py` — frontmatter-based index (different layer)
- `pipeline/obsidian_bridge.py` — text-similarity search over note bodies
- CLOSED_LOOP_ARCHITECTURE.md Session #20 — the full design rationale
