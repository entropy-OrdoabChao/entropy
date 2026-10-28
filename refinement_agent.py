"""
refinement_agent.py — Self-Improving Legal Reasoning Loop

Background process that cycles through the entire legal database:
  1. Read provisions from Obsidian vault + ChromaDB
  2. Run all engines (burden, trigger, remedy, immunity, actor-scope, conformity)
  3. Write enriched frontmatter back to vault nodes
  4. Detect gaps → trigger scrapers
  5. Log improvements → repeat

Does NOT modify ChromaDB (immutable rule).
Does NOT use LLM for legal conclusions.
Writes ONLY to Obsidian vault frontmatter and refinement logs.

Usage:
    python refinement_agent.py              # full run (skips already-refined)
    python refinement_agent.py --pass burden   # single pass
    python refinement_agent.py --dry-run       # report only, no writes
    python refinement_agent.py --force         # re-process already-refined nodes
    python refinement_agent.py --pass gap_detect --limit 500  # run slow pass separately
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os
import sys
import json
import re
import time
import yaml
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
from core.logger import get_logger

log = get_logger("refinement_agent")

# ─── Config ──────────────────────────────────────────────────────────

ROOT = Path(os.getenv("BEEZLE_ROOT", str(_eroot())))
VAULT_DIR = ROOT / "obsidian_sync" / "primary_sources"
LOGS_DIR = ROOT / "logs" / "refinement"
TEMPLATES_DIR = ROOT / "obsidian_sync" / "templates"

PASSES = [
    "classify",
    "burden",
    "trigger",
    "remedy",
    "actor_scope",
    "conformity",
    "construction",
    "cross_ref",
    "gap_detect",
]

# gap_detect is O(n²) — run separately with: python refinement_agent.py --pass gap_detect --limit 500
# immunity_map is per-defendant, not per-node — skipped automatically


# ─── Vault Node I/O ─────────────────────────────────────────────────

def read_vault_node(path: Path) -> dict:
    """Read an Obsidian vault node, parsing YAML frontmatter and body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    node = {"path": str(path), "frontmatter": {}, "body": "", "raw": text}

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                node["frontmatter"] = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                node["frontmatter"] = {}
            node["body"] = parts[2].strip()
        else:
            node["body"] = text
    else:
        node["body"] = text

    return node


def write_vault_frontmatter(path: Path, frontmatter: dict, body: str):
    """Write updated frontmatter to a vault node, preserving body."""
    fm_text = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    output = f"---\n{fm_text}---\n\n{body}"
    path.write_text(output, encoding="utf-8")


def iter_vault_nodes(jurisdiction: str = None) -> List[Path]:
    """Iterate over all vault nodes, optionally filtered by jurisdiction."""
    if not VAULT_DIR.exists():
        log.warning(f"Vault directory not found: {VAULT_DIR}")
        return []

    nodes = []
    for subdir in VAULT_DIR.iterdir():
        if not subdir.is_dir():
            continue
        if jurisdiction and subdir.name != jurisdiction:
            continue
        for md_file in subdir.glob("*.md"):
            nodes.append(md_file)
    return sorted(nodes)


# ─── Individual Passes ───────────────────────────────────────────────

def run_burden_pass(node: dict, dry_run: bool = False) -> dict:
    """Run burden engine on a vault node."""
    from pipeline.burden_engine import analyze_burden
    body = node["body"]
    citation = node["frontmatter"].get("source_id", "")
    if not body.strip():
        return {}
    analysis = analyze_burden(body, citation)
    return analysis.to_frontmatter()


def run_trigger_pass(node: dict, dry_run: bool = False) -> dict:
    """Run trigger engine on a vault node."""
    from pipeline.trigger_engine import extract_triggers
    body = node["body"]
    citation = node["frontmatter"].get("source_id", "")
    if not body.strip():
        return {}
    analysis = extract_triggers(body, citation)
    return analysis.to_frontmatter()


def run_remedy_pass(node: dict, dry_run: bool = False) -> dict:
    """Run remedy engine on a vault node."""
    from pipeline.remedy_engine import extract_remedies
    body = node["body"]
    citation = node["frontmatter"].get("source_id", "")
    if not body.strip():
        return {}
    analysis = extract_remedies(body, citation)
    return analysis.to_frontmatter()


def run_actor_scope_pass(node: dict, dry_run: bool = False) -> dict:
    """Run actor-scope engine on a vault node."""
    from pipeline.actor_scope_engine import analyze_actor_scope
    body = node["body"]
    citation = node["frontmatter"].get("source_id", "")
    if not body.strip():
        return {}
    analysis = analyze_actor_scope(body, citation)
    return analysis.to_frontmatter()


def run_conformity_pass(node: dict, dry_run: bool = False) -> dict:
    """Run conformity check — determine authority level and trace enactment."""
    from pipeline.conformity_checker import detect_authority_level, trace_enactment
    citation = node["frontmatter"].get("source_id", "")
    body = node["body"]
    level = detect_authority_level(citation)
    trace = trace_enactment(citation, body)
    result = {
        "authority_level": level,
    }
    if trace.public_law:
        result["public_law"] = trace.public_law
    if trace.constitutional_authority:
        result["constitutional_authority"] = trace.constitutional_authority
    if trace.enactment_date:
        result["enactment_date"] = trace.enactment_date
    return result


def run_cross_ref_pass(node: dict, dry_run: bool = False) -> dict:
    """Extract cross-references and build wikilinks."""
    body = node["body"]
    citations = extract_citations(body)
    if not citations:
        return {}
    return {"cross_references": citations[:20]}


def run_construction_pass(node: dict, dry_run: bool = False) -> dict:
    """Run statutory construction rules on a vault node."""
    from pipeline.statutory_construction import apply_construction_rules
    body = str(node.get("body", "") or "")
    citation = str(node.get("frontmatter", {}).get("source_id", "") or "")
    if not body.strip():
        return {}
    analysis = apply_construction_rules(body, citation)
    fm = analysis.to_frontmatter()
    # Only return if rules were actually triggered
    if fm.get("construction_rules"):
        return fm
    return {}


def run_gap_detect_pass(node: dict, dry_run: bool = False) -> dict:
    """Detect references to provisions not in the vault.

    Uses pre-built _VAULT_INDEX for O(1) lookups instead of scanning filesystem.
    """
    body = node["body"]
    citations = extract_citations(body)
    if not citations:
        return {}
    missing = []
    for cite in citations:
        normalized = cite.lower().replace(" ", "_").replace("§", "sec")
        found = any(normalized in stem for stem in _VAULT_INDEX)
        if not found:
            missing.append(cite)

    if missing:
        return {"missing_authorities": missing[:10]}
    return {}


# Pre-built vault filename index — populated once in run_refinement()
_VAULT_INDEX = set()


# ─── Citation Extraction ────────────────────────────────────────────

CITATION_PATTERNS = [
    r"\d+\s+U\.?\s*S\.?\s*C\.?\s*§\s*\d+[a-z]*(?:\([a-z0-9]+\))*",
    r"\d+\s+C\.?\s*F\.?\s*R\.?\s*§\s*\d+(?:\.\d+)?",
    r"\d+\s+Pa\.?\s*(?:C\.?\s*S\.?|Stat\.?)\s*§\s*\d+",
    r"Pub(?:lic)?\s*\.?\s*L(?:aw)?\.?\s*\d+[\s-]\d+",
]


def extract_citations(text: str) -> List[str]:
    """Extract legal citations from text."""
    citations = []
    seen = set()
    for pattern in CITATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            cite = match.group().strip()
            if cite not in seen:
                seen.add(cite)
                citations.append(cite)
    return citations




def run_classify_pass(node: dict, dry_run: bool = False) -> dict:
    """Classify source type and authority level from filename, URL, title, source_id.

    Fixes the misclassification problem where Treasury letters are tagged as 'statute',
    Federal Register notices as 'statute', public laws as 'legal_document', etc.

    Only reclassifies generic/wrong types (legal_document, unknown, rebuttal).
    Does NOT override already-correct classifications unless clearly wrong.
    """
    import re as _re
    fm = node.get("frontmatter", {})
    path = Path(node.get("path", ""))
    fn = path.stem.lower() if path.stem else ""
    old_type = str(fm.get("type", "unknown")).lower()
    old_auth = str(fm.get("authority_level", "unknown")).lower()
    url = str(fm.get("url", "")).lower()
    title = str(fm.get("title", "")).lower()
    source_id = str(fm.get("source_id", "")).lower()

    new_type = None
    new_auth = None

    # ─── 1. FILENAME PATTERNS (highest confidence) ───

    if fn.startswith("plaw-") or fn.startswith("plaw_"):
        new_type, new_auth = "public_law", "statute"
    elif fn.startswith("uscode-") or fn.startswith("usc_title"):
        new_type, new_auth = "statute", "statute"
    elif fn.startswith("statute-"):
        new_type, new_auth = "statute", "statute"
    elif fn.startswith("cfr-") or fn.startswith("ecfr-") or fn.startswith("ecfr_"):
        new_type, new_auth = "regulation", "regulation"
    elif fn.startswith("frcp_rule") or fn.startswith("frap_rule"):
        new_type, new_auth = "court_rule", "court_rule"
    elif fn.startswith("local_rule") or fn.startswith("pa_r_"):
        new_type, new_auth = "court_rule", "court_rule"
    elif fn.startswith("irm_"):
        new_type, new_auth = "agency_guidance", "regulation"
    elif fn.startswith("uscourts-") or fn.startswith("gov_uscourts"):
        new_type, new_auth = "case_law", "case_law"
    elif fn.startswith("usreports-"):
        new_type, new_auth = "case_law", "case_law"
    elif fn.startswith("bills-"):
        new_type, new_auth = "bill", "legislation"
    elif fn.startswith("occ_") or fn.startswith("occ-"):
        new_type, new_auth = "agency_guidance", "regulation"
    elif fn.startswith("fdic_") or fn.startswith("fdic-"):
        new_type, new_auth = "agency_guidance", "regulation"
    elif fn.startswith("sec_") or fn.startswith("sec-"):
        new_type, new_auth = "agency_guidance", "regulation"
    elif fn.startswith("eo_") or fn.startswith("eo-") or "executive_order" in fn or "executive-order" in fn:
        new_type, new_auth = "executive_order", "executive_order"
    elif "aba_model" in fn or "judicial_conduct" in fn or "canon" in fn:
        new_type, new_auth = "rule", "court_rule"
    elif _re.match(r"vol\d", fn):
        new_type, new_auth = "agency_guidance", "agency_guidance"
    elif fn.startswith("treas") or fn.startswith("treasury"):
        new_type, new_auth = "agency_guidance", "agency_guidance"

    # ─── 2. URL PATTERNS ───

    if not new_type and url:
        if "law.cornell.edu/rules" in url:
            new_type, new_auth = "court_rule", "court_rule"
        elif "law.cornell.edu/uscode" in url:
            new_type, new_auth = "statute", "statute"
        elif "law.cornell.edu/cfr" in url:
            new_type, new_auth = "regulation", "regulation"
        elif "law.cornell.edu/ethics" in url:
            new_type, new_auth = "rule", "court_rule"
        elif "ecfr.gov" in url:
            new_type, new_auth = "regulation", "regulation"
        elif "/plaw-" in url:
            new_type, new_auth = "public_law", "statute"
        elif "/uscode-" in url:
            new_type, new_auth = "statute", "statute"
        elif "/cfr-" in url or "/ecfr-" in url:
            new_type, new_auth = "regulation", "regulation"
        elif "irs.gov/irm" in url:
            new_type, new_auth = "agency_guidance", "regulation"
        elif "irs.gov" in url:
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "occ.gov" in url or "occ.treas.gov" in url:
            new_type, new_auth = "agency_guidance", "regulation"
        elif "fdic.gov" in url:
            new_type, new_auth = "agency_guidance", "regulation"
        elif "sec.gov" in url:
            new_type, new_auth = "agency_guidance", "regulation"
        elif "fincen.gov" in url:
            new_type, new_auth = "agency_guidance", "regulation"
        elif "treasury.gov" in url:
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "pacourts.us" in url:
            new_type, new_auth = "court_rule", "court_rule"

    # ─── 3. TITLE PATTERNS ───

    if not new_type and title:
        if _re.search(r"in the (?:supreme|superior|commonwealth|district|circuit|appellate) court", title):
            new_type, new_auth = "case_law", "case_law"
        elif "federal register" in title:
            if "proposed rule" in title or "rules and regulations" in title:
                new_type, new_auth = "regulation", "regulation"
            elif "notice" in title:
                new_type, new_auth = "agency_guidance", "agency_guidance"
            else:
                new_type, new_auth = "regulation", "regulation"
        elif title.startswith("public law") or "pub. l." in title:
            new_type, new_auth = "public_law", "statute"
        elif "federal rules of" in title or "rules of appellate procedure" in title or "rules of civil procedure" in title:
            new_type, new_auth = "court_rule", "court_rule"
        elif "model code" in title or "judicial conduct" in title:
            new_type, new_auth = "rule", "court_rule"
        elif title.startswith("department of"):
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif any(a in title for a in [
            "office of the comptroller", "federal deposit insurance",
            "securities and exchange", "consumer financial protection",
            "financial crimes enforcement", "office of foreign assets",
        ]):
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "treasury" in title and any(w in title for w in ["letter", "report", "guidance", "bulletin"]):
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "constitution" in title and ("united states" in title or "amendment" in title):
            new_type, new_auth = "constitution", "constitution"

    # ─── 4. SOURCE_ID FALLBACK ───

    if not new_type and source_id:
        if _re.match(r"\d{4}-\d{4,6}$", source_id):
            new_type, new_auth = "regulation", "regulation"
        elif source_id.startswith("plaw_"):
            new_type, new_auth = "public_law", "statute"
        elif source_id.startswith("usc_title"):
            new_type, new_auth = "statute", "statute"

    # ─── 5. Existing authority_level hint ───

    if not new_type and old_auth != "unknown":
        if old_auth == "agency_guidance":
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif old_auth == "constitution":
            new_type, new_auth = "constitution", "constitution"
        elif old_auth == "court_rule":
            new_type, new_auth = "court_rule", "court_rule"
        elif old_auth == "treaty":
            new_type, new_auth = "treaty", "treaty"

    # ─── APPLY ───

    if not new_type:
        return {}


    # ─── 3. TITLE / CONTENT KEYWORD PATTERNS (fallback for numeric-prefix files) ───

    if not new_type and title:
        import re as _re2
        t = title.lower()
        # Court rules
        if _re2.search(r"^rule \d", t) or "rules of " in t or "ruletext" in fn:
            new_type, new_auth = "court_rule", "court_rule"
        elif "appellate" in t and ("rule" in t or "ruletext" in fn):
            new_type, new_auth = "court_rule", "court_rule"
        elif "frcp" in t or "frap" in t or "federal rule" in t:
            new_type, new_auth = "court_rule", "court_rule"
        elif "local rule" in t or "standing order" in t:
            new_type, new_auth = "court_rule", "court_rule"
        # Statutes
        elif _re2.search(r"\d+\s*u\.?s\.?c", t) or "united states code" in t:
            new_type, new_auth = "statute", "statute"
        elif _re2.search(r"\d+\s*pa\.?\s*c\.?s", t) or "pennsylvania consolidated" in t:
            new_type, new_auth = "statute", "statute"
        elif "public law" in t or "pub. l." in t or "pub.l." in t:
            new_type, new_auth = "public_law", "statute"
        # Regulations
        elif _re2.search(r"\d+\s*c\.?f\.?r", t) or "code of federal regulations" in t:
            new_type, new_auth = "regulation", "regulation"
        elif "federal register" in t or "fed. reg." in t:
            new_type, new_auth = "regulation", "regulation"
        # Case law
        elif _re2.search(r"v\.\s", t) and _re2.search(r"\d{4}", t):
            new_type, new_auth = "case_law", "case_law"
        elif "opinion" in t and ("court" in t or "circuit" in t or "district" in t):
            new_type, new_auth = "case_law", "case_law"
        # Agency guidance
        elif any(kw in t for kw in ["irm ", "irs ", "treasury ", "occ ", "fdic ", "sec ", "cfpb "]):
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "bulletin" in t or "advisory" in t or "guidance" in t or "circular" in t:
            new_type, new_auth = "agency_guidance", "agency_guidance"
        elif "internal revenue manual" in t or "revenue ruling" in t or "revenue procedure" in t:
            new_type, new_auth = "agency_guidance", "agency_guidance"
        # Canons / Ethics
        elif "canon" in t or "judicial conduct" in t or "ethics" in t or "aba model" in t:
            new_type, new_auth = "rule", "court_rule"
        # Executive orders
        elif "executive order" in t:
            new_type, new_auth = "executive_order", "executive_order"
        # Education / non-legal (like Spanish transcripts)
        elif any(kw in t for kw in ["transcript", "video", "education", "worksheet", "brochure", "newsletter"]):
            new_type, new_auth = "non_legal", "none"

    # ─── 4. FILENAME KEYWORD FALLBACK (numeric prefix files) ───

    if not new_type:
        if "ruletext" in fn or "rule-text" in fn:
            new_type, new_auth = "court_rule", "court_rule"
        elif "appellate" in fn:
            new_type, new_auth = "court_rule", "court_rule"
        elif "opinion" in fn:
            new_type, new_auth = "case_law", "case_law"
        elif "statute" in fn:
            new_type, new_auth = "statute", "statute"
        elif "regulation" in fn or "cfr" in fn:
            new_type, new_auth = "regulation", "regulation"
        elif "order" in fn and "executive" not in fn:
            new_type, new_auth = "court_order", "case_law"
        elif "transcript" in fn or "video" in fn or "spanish" in fn:
            new_type, new_auth = "non_legal", "none"


    updates = {}

    # Only reclassify generic/wrong types
    reclassify_from = {"legal_document", "unknown", "rebuttal"}
    # Also fix statutes that are actually agency guidance or public laws
    if old_type in reclassify_from:
        updates["type"] = new_type
    elif old_type == "statute" and new_type in ("public_law", "agency_guidance", "regulation", "case_law"):
        updates["type"] = new_type
    elif old_type == "case_law" and new_type in ("agency_guidance", "regulation") and "court" not in title:
        updates["type"] = new_type

    # Always fix unknown authority_level
    if old_auth == "unknown" and new_auth:
        updates["authority_level"] = new_auth

    return updates


# ─── Pass Router ─────────────────────────────────────────────────────



def run_authority_fix_pass(node: dict, dry_run: bool = False) -> dict:
    """Fix missing authority_level based on type field."""
    fm = node.get("frontmatter", {})
    old_auth = str(fm.get("authority_level", "")).strip()
    node_type = str(fm.get("type", "")).strip()
    
    if old_auth and old_auth not in ("missing", "unknown", ""):
        return {}
    
    type_to_auth = {
        "statute": "statute",
        "regulation": "regulation",
        "regulation/federal": "regulation",
        "court_rule": "court_rule",
        "rule": "court_rule",
        "case_law": "case_law",
        "public_law": "statute",
        "agency_guidance": "agency_guidance",
        "executive_order": "executive_order",
        "reference": "none",
        "non_legal": "none",
        "ocr_fragment": "none",
        "court_form": "none",
        "legal_document": "unknown",
    }
    
    new_auth = type_to_auth.get(node_type)
    if new_auth and new_auth != old_auth:
        return {"authority_level": new_auth}
    return {}


def run_cross_link_pass(node: dict, dry_run: bool = False) -> dict:
    """Build cross-links between vault nodes based on citations in text.
    
    Finds references to other legal sources in the node text and adds
    Obsidian [[wikilinks]] to the frontmatter as a cross_refs list.
    
    Links built:
      - Statute → implementing CFR regulation (e.g. 26 USC → 26 CFR)
      - Regulation → authorizing statute (e.g. 26 CFR → 26 USC)  
      - Case law → statute it interprets
      - Court rule → authorizing statute
      - Any node → cited cases (v. pattern)
    """
    import re as _re
    fm = node.get("frontmatter", {})
    body = node.get("body", "")
    node_type = str(fm.get("type", ""))
    
    # Only process classified legal nodes
    if node_type in ("non_legal", "ocr_fragment", "court_form", "reference", "missing", ""):
        return {}
    
    text = body[:5000]  # only scan first 5000 chars for performance
    cross_refs = []
    
    # 1. USC citations: "26 U.S.C. § 3401" or "42 U.S.C. 1983" or "15 USC 1681"
    for m in _re.finditer(r"(\d+)\s*U\.?S\.?C\.?\s*(?:§\s*)?(\d+[a-z]?(?:\([a-z0-9]+\))*)", text, _re.I):
        title, section = m.group(1), m.group(2)
        ref = "USC_title" + title + "_sec" + section
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 2. CFR citations: "26 C.F.R. § 31.3401" or "12 CFR 1024"
    for m in _re.finditer(r"(\d+)\s*C\.?F\.?R\.?\s*(?:§\s*)?(\d+(?:\.\d+)*)", text, _re.I):
        title, section = m.group(1), m.group(2)
        ref = "CFR_title" + title + "_sec" + section
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 3. Pa.C.S. citations: "42 Pa.C.S. § 8371"
    for m in _re.finditer(r"(\d+)\s*Pa\.?\s*C\.?S\.?\s*(?:§\s*)?(\d+[a-z]?)", text, _re.I):
        title, section = m.group(1), m.group(2)
        ref = "PaCS_title" + title + "_sec" + section
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 4. P.S. citations: "73 P.S. § 201-9.2"
    for m in _re.finditer(r"(\d+)\s*P\.?S\.?\s*(?:§\s*)?(\d+(?:-[\d.]+)?)", text, _re.I):
        title, section = m.group(1), m.group(2)
        ref = "PS_title" + title + "_sec" + section
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 5. FRCP / FRAP rules: "Rule 12(b)(6)" or "FRCP 56"
    for m in _re.finditer(r"(?:FRCP|FRAP|Fed\.?\s*R\.?\s*(?:Civ|App)\.?\s*P\.?)\s*(?:Rule\s*)?(\d+)", text, _re.I):
        ref = "FRCP_rule" + m.group(1)
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 6. Pa.R.C.P. rules: "Pa.R.C.P. 1024"
    for m in _re.finditer(r"Pa\.?\s*R\.?\s*C\.?\s*P\.?\s*(\d+)", text, _re.I):
        ref = "PaRCP_rule" + m.group(1)
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 7. Case citations: "Smith v. Jones, 550 U.S. 544 (2007)"
    for m in _re.finditer(r"([A-Z][a-z]+)\s+v\.\s+([A-Z][a-z]+).*?(\d+)\s+(?:U\.S\.|F\.\d+d?|S\.Ct\.|Pa\.)\s+(\d+)", text):
        ref = m.group(1) + "_v_" + m.group(2) + "_" + m.group(3)
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    # 8. Public Law: "Pub. L. 112-29" or "Public Law 117-286"
    for m in _re.finditer(r"(?:Pub(?:lic)?\.?\s*L(?:aw)?\.?)\s*(\d+-\d+)", text, _re.I):
        ref = "PLAW_" + m.group(1)
        if ref not in cross_refs:
            cross_refs.append(ref)
    
    if not cross_refs:
        return {}
    
    # Cap at 50 cross-refs per node
    cross_refs = cross_refs[:50]
    
    return {"cross_refs": cross_refs, "cross_ref_count": len(cross_refs)}


PASS_FUNCTIONS = {
    "classify": run_classify_pass,
    "burden": run_burden_pass,
    "trigger": run_trigger_pass,
    "remedy": run_remedy_pass,
    "actor_scope": run_actor_scope_pass,
    "conformity": run_conformity_pass,
    "construction": run_construction_pass,
    "cross_ref": run_cross_ref_pass,
    "gap_detect": run_gap_detect_pass,
    "authority_fix": run_authority_fix_pass,
    "cross_link": run_cross_link_pass,
}


# ─── Main Loop ───────────────────────────────────────────────────────

def run_refinement(passes: List[str] = None, dry_run: bool = False,
                   jurisdiction: str = None, limit: int = 0,
                   force: bool = False) -> dict:
    """
    Run the refinement loop.

    Args:
        passes: list of pass names to run (default: all)
        dry_run: if True, report only, don't write
        jurisdiction: filter to specific jurisdiction folder
        limit: max nodes to process (0 = all)
        force: if True, re-process already-refined nodes

    Returns:
        Summary dict with counts and gaps found.
    """
    passes = passes or PASSES
    nodes = iter_vault_nodes(jurisdiction)
    if limit > 0:
        nodes = nodes[:limit]

    log.info(f"Refinement starting: {len(nodes)} nodes, passes: {passes}, dry_run: {dry_run}, force: {force}")

    # Build vault filename index for gap_detect (O(1) lookups)
    global _VAULT_INDEX
    if "gap_detect" in passes and not _VAULT_INDEX:
        print("  Building vault filename index...", flush=True)
        for subdir in VAULT_DIR.iterdir():
            if not subdir.is_dir():
                continue
            for md_file in subdir.glob("*.md"):
                _VAULT_INDEX.add(md_file.stem.lower())
        print(f"  Indexed {len(_VAULT_INDEX):,} vault filenames", flush=True)

    summary = {
        "started": datetime.now().isoformat(),
        "nodes_total": len(nodes),
        "nodes_processed": 0,
        "nodes_updated": 0,
        "nodes_skipped": 0,
        "passes_run": passes,
        "gaps_found": [],
        "errors": [],
        "dry_run": dry_run,
    }

    for i, node_path in enumerate(nodes):
        if (i + 1) % 100 == 0:
            pct = (i + 1) * 100 // len(nodes)
            log.info(f"Progress: {i+1}/{len(nodes)}")
            print(f"  [{i+1:,}/{len(nodes):,}] {pct}% | "
                  f"Updated: {summary['nodes_updated']} | "
                  f"Skipped: {summary['nodes_skipped']} | "
                  f"Errors: {len(summary['errors'])}", flush=True)

        try:
            node = read_vault_node(node_path)
            fm = node["frontmatter"]

            # Skip already-refined nodes unless --force
            if not force and fm.get("refinement_version"):
                summary["nodes_skipped"] += 1
                summary["nodes_processed"] += 1
                continue

            updated = False

            for pass_name in passes:
                if pass_name == "immunity_map":
                    continue  # immunity is per-defendant, not per-node
                func = PASS_FUNCTIONS.get(pass_name)
                if not func:
                    continue

                try:
                    result = func(node, dry_run)
                    if result:
                        if pass_name == "classify":
                            # classify returns top-level keys (type, authority_level)
                            fm.update(result)
                        else:
                            # Other passes nest under pass-specific key
                            fm[pass_name] = result
                        updated = True

                        # Track gaps
                        if "missing_authorities" in result:
                            for gap in result["missing_authorities"]:
                                if gap not in summary["gaps_found"]:
                                    summary["gaps_found"].append(gap)

                except Exception as e:
                    summary["errors"].append(f"{node_path.name}/{pass_name}: {str(e)[:100]}")

            # Write back if updated
            if updated and not dry_run:
                fm["refinement_version"] = datetime.now().strftime("%Y%m%d")
                write_vault_frontmatter(node_path, fm, node["body"])
                summary["nodes_updated"] += 1

            summary["nodes_processed"] += 1

        except Exception as e:
            summary["errors"].append(f"{node_path.name}: {str(e)[:100]}")

    summary["finished"] = datetime.now().isoformat()

    # Write log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"refinement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_path, "w") as f:
        json.dump(summary, f, indent=2)
    log.info(f"Refinement complete: {summary['nodes_processed']} processed, "
             f"{summary['nodes_updated']} updated, {summary['nodes_skipped']} skipped, "
             f"{len(summary['gaps_found'])} gaps, {len(summary['errors'])} errors")
    log.info(f"Log: {log_path}")

    return summary


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Beezle Legal Refinement Agent")
    parser.add_argument("--pass", dest="single_pass", help="Run a single pass",
                        choices=list(PASS_FUNCTIONS.keys()))
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't write")
    parser.add_argument("--force", action="store_true", help="Re-process already-refined nodes")
    parser.add_argument("--jurisdiction", help="Filter to jurisdiction (e.g., federal, pennsylvania)")
    parser.add_argument("--limit", type=int, default=0, help="Max nodes to process")
    args = parser.parse_args()

    passes = [args.single_pass] if args.single_pass else None
    summary = run_refinement(
        passes=passes,
        dry_run=args.dry_run,
        jurisdiction=args.jurisdiction,
        limit=args.limit,
        force=args.force,
    )

    print(f"\n{'='*50}")
    print(f"REFINEMENT COMPLETE")
    print(f"{'='*50}")
    print(f"Nodes total:     {summary['nodes_total']:,}")
    print(f"Nodes processed: {summary['nodes_processed']:,}")
    print(f"Nodes updated:   {summary['nodes_updated']:,}")
    print(f"Nodes skipped:   {summary['nodes_skipped']:,}")
    print(f"Gaps found:      {len(summary['gaps_found'])}")
    print(f"Errors:          {len(summary['errors'])}")
    if summary["gaps_found"]:
        print(f"\nMissing authorities (top 10):")
        for gap in summary["gaps_found"][:10]:
            print(f"  - {gap}")


if __name__ == "__main__":
    main()
