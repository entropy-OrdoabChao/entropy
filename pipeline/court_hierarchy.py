import re

# Auto-generated stub. Real court hierarchy data lives in ChromaDB.
COURT_HIERARCHY = {
    "scotus": 10, "us_supreme_court": 10,
    "circuit": 8, "us_circuit": 8, "ca3": 8,
    "district": 6, "us_district": 6, "edpa": 6,
    "state_supreme": 5, "pa_supreme": 5,
    "state_superior": 4, "pa_superior": 4,
    "state_trial": 3, "phila_ccp": 3,
    "agency": 2, "other": 1,
}
def get_court_rank(name):
    if not name: return 0
    key = str(name).lower().replace(" ", "_")
    for k, v in COURT_HIERARCHY.items():
        if k in key: return v
    return 1
# BEEZLE_FN_UNUSED 2026-06-13: compare_courts (no static refs in pipeline/)
def compare_courts(a, b):
    return get_court_rank(a) - get_court_rank(b)
# ══════════════════════════════════════════════════════════════════════════════
# ARGUMENT ORDER IS THE CONTRACT — (CITED, TARGET), ALWAYS
#
# Defect found 2026-10-09 (BACKLOG_system E15). `is_binding` declared
# `(citing_court, cited_court)` while EVERY live call site passes
# `(cited, target)`:
#     beezle_mcp_server.py:248      is_binding(cited_court, target_court)
#     pipeline/authority_miner.py   is_binding(cited_court, "EDPA" | "PA_CP_PHILA" | "PA_SUP")
#     pipeline/authority_validity.py:337  is_binding(result.cited_court, case_court)
#     pipeline/court_hierarchy.audit_citation(citation, target_court)
# so every caller was reading the hierarchy BACKWARDS. Measured on live:
#     is_binding("SCOTUS","PA_CP_PHILA")   -> False   (art. VI says otherwise)
#     is_binding("CA3","EDPA")             -> False   (the circuit over its own district)
#     is_binding("PA_SUPREME","PA_CP_PHILA")-> False  (the reported E15 symptom)
#     is_binding("PA_CP_PHILA","PA_SUPREME")-> True   (a trial court "binding" the high court)
# i.e. the whole binding layer answered False for real authority and True for
# the inversion. The reported symptom was one cell of a fully transposed table.
#
# CLASS FIX, not a one-off:
#   1. ONE decision engine, `binding_decision(cited_court, target_court)`. Both
#      `is_binding` and `audit_citation` call it, so the two organs can no longer
#      hold different answers for the same pair (they held identical rules in two
#      hand-written copies before; that duplication is what let one get transposed).
#   2. CANONICAL ORDER = (cited, target) everywhere — it is what audit_citation
#      and all four call sites already use, and it reads the way the question is
#      asked: "does CITED bind TARGET?"
#   3. The stale keyword `citing_court=` now raises TypeError naming the canonical
#      order. A caller written against the old signature fails LOUDLY instead of
#      silently inverting the hierarchy. `binds(...)` is keyword-only for new code.
#   4. The relations themselves are UNCHANGED and are DATA (`_BINDING_RULES`,
#      `BINDING_FIXTURES`): a new court or rule needs a table row, not code.
#      [corpus] U.S. Const. art. VI cl. 2, chunk 017784d9ab2d905b70ce6ce4c3d090d0:
#      "shall be the supreme Law of the Land; and the Judges in every State shall
#      be bound thereby, any Thing in the Constitution or Laws of any State to the
#      contrary notwithstanding." -> SCOTUS/federal law binds state judges, which
#      is the `("state", "supreme_court") -> True` row.
#      [corpus] SupremeCourt101_January2026, chunk 0d693c14fed2d7554f0ede11229ea62b:
#      "The majority opinion is binding on all lower courts" -> vertical stare decisis.
#      NOT IN CORPUS (substring sweep, 11 phrasings): an express statement that PA
#      Supreme / Superior decisions bind the courts of common pleas. Those rows are
#      carried over verbatim from the pre-existing audit_citation table and are NOT
#      asserted as new law by this change; a semantic pull is filed as a [seat:proof]
#      card. This change moves no relation — it only stops reading them backwards.
# ══════════════════════════════════════════════════════════════════════════════

_SAME_CIRCUIT = "same_circuit"

# (target_jurisdiction, cited_level) -> True | False | tuple-of-target-levels | _SAME_CIRCUIT
# Carried verbatim from the two previous hand-written copies (is_binding +
# audit_citation), which agreed on every cell. Add a court/rule HERE, not in code.
_BINDING_RULES = {
    ("state", "supreme_court"):      True,          # art. VI, on federal questions
    ("state", "statute"):            True,          # federal statute likewise
    ("state", "state_supreme"):      ("state_trial", "state_intermediate", "state_supreme"),
    ("state", "state_intermediate"): ("state_trial", "state_intermediate"),
    ("federal", "supreme_court"):    True,
    ("federal", "statute"):          True,
    ("federal", "circuit_court"):    _SAME_CIRCUIT,  # a sister circuit is persuasive only
}

_NOT_BINDING_REASON = {
    ("federal", "district_court"):     "a district-court opinion binds no court",
    ("federal", "state_supreme"):      ("a state court does not bind a federal court, though it is "
                                        "authoritative on that state's substantive law under Erie"),
    ("federal", "state_intermediate"): ("a state court does not bind a federal court, though it is "
                                        "authoritative on that state's substantive law under Erie"),
    ("federal", "state_trial"):        ("a state court does not bind a federal court, though it is "
                                        "authoritative on that state's substantive law under Erie"),
    ("federal", "state_other"):        ("a state court does not bind a federal court, though it is "
                                        "authoritative on that state's substantive law under Erie"),
    ("state", "circuit_court"):        ("a federal court of appeals/district court does not bind a "
                                        "state court on questions of state law"),
    ("state", "district_court"):       ("a federal court of appeals/district court does not bind a "
                                        "state court on questions of state law"),
    ("state", "state_trial"):          "a coordinate trial court is not binding precedent",
}

# GUARDRAIL DATA. (cited, target, expected) — the probe asserts every row, so a
# transposition can never again pass the gate. Extend the table, not the test code.
BINDING_FIXTURES = (
    ("SCOTUS",      "PA_CP_PHILA", True),   # art. VI [corpus]
    ("SCOTUS",      "EDPA",        True),
    ("PA_SUPREME",  "PA_CP_PHILA", True),   # E15, the reported symptom
    ("PA_SUP",      "PA_CP_PHILA", True),   # PA_SUP == the SUPREME court here
    ("PA_SUPER",    "PA_CP_PHILA", True),
    ("PA_SUPER",    "PA_SUPREME",  False),  # intermediate does not bind the high court
    ("PA_CP_PHILA", "PA_SUPREME",  False),  # the old inverted TRUE
    ("PA_CP_PHILA", "PA_CP_OTHER", False),  # coordinate trial court
    ("CA3",         "EDPA",        True),   # the circuit over its own district
    ("CA3",         "PA_CP_PHILA", False),  # no federal circuit binds a state court
    ("EDPA",        "PA_CP_PHILA", False),
    ("EDPA",        "CA3",         False),  # a district opinion binds no court
    ("PA_SUPREME",  "EDPA",        False),  # Erie-authoritative, not binding
    ("CA3",         "WDPA",        True),   # same circuit
)


def binding_decision(cited_court: str, target_court: str) -> dict:
    """Does CITED_COURT bind TARGET_COURT? Single source of truth.

    Order is (cited, target) — the question read left to right. Returns
    {binding, reason, cited_level, cited_jurisdiction, target_level,
     target_jurisdiction}. `binding` is False whenever either side is
     unresolvable: callers must FAIL CLOSED, never read "unknown" as "binding".
    """
    cit = classify_court(cited_court)
    tgt = classify_court(target_court) if target_court else {}
    c_level = cit.get("level", "unknown")
    t_juris = tgt.get("jurisdiction", "")
    t_level = tgt.get("level", "")
    out = {
        "binding": False, "reason": "",
        "cited_level": c_level, "cited_jurisdiction": cit.get("jurisdiction", ""),
        "target_level": t_level, "target_jurisdiction": t_juris,
    }
    if c_level == "unknown" or not t_juris:
        out["reason"] = "court not resolved; failing closed"
        return out

    rule = _BINDING_RULES.get((t_juris, c_level))
    if rule is None:
        out["reason"] = _NOT_BINDING_REASON.get((t_juris, c_level), "")
        return out
    if rule is _SAME_CIRCUIT:
        cc, tc = _circuit_of(cited_court), _circuit_of(target_court)
        out["binding"] = bool(cc) and cc == tc
        if not out["binding"]:
            out["reason"] = "a sister circuit is persuasive only"
        return out
    if isinstance(rule, tuple):
        out["binding"] = t_level in rule
        if not out["binding"]:
            out["reason"] = "an intermediate appellate court does not bind a higher court" \
                if c_level == "state_intermediate" else "the cited court does not bind this target"
        return out
    out["binding"] = bool(rule)
    return out


def is_binding(cited_court=None, target_court=None, **kw):
    """Does CITED_COURT bind TARGET_COURT?  Order: (cited, target).

    NOT a rank comparison. An earlier body was
        get_court_rank(cited) >= get_court_rank(citing)
    which -- with district=6 above state_supreme=5 -- held that a federal district
    court binds the Pennsylvania Supreme Court. Federal and state are distinct
    precedential systems; only SCOTUS and federal statutes reach across (art. VI).

    `citing_court=` is REFUSED: it was the old parameter name for the TARGET, in
    the opposite position, and silently inverted the hierarchy for every caller.
    """
    if "citing_court" in kw:
        raise TypeError(
            "is_binding(): 'citing_court' was removed 2026-10-09 (it named the TARGET "
            "and sat in the CITED position, inverting every answer). Canonical order is "
            "is_binding(cited_court, target_court) -- 'does CITED bind TARGET?'. "
            "Use binds(cited_court=..., target_court=...) to be explicit."
        )
    if kw:
        raise TypeError(f"is_binding(): unexpected keyword(s) {sorted(kw)}")
    return binding_decision(cited_court, target_court)["binding"]


def binds(*, cited_court: str, target_court: str) -> bool:
    """Keyword-only form of is_binding — for new call sites, order-proof."""
    return binding_decision(cited_court, target_court)["binding"]


# ============================================================================
# Stubs added by hotfix_v11 — minimal implementations to satisfy imports
# from hierarchy_enforcer.py and authority_validity.py.
# Real logic should replace these once court_hierarchy is fully designed.
# ============================================================================

def classify_court(citation_or_court_name: str) -> dict:
    """
    Classify a citation or court name into a standardized authority level.
    Returns {level, jurisdiction, binding_in, persuasive_in}.
    Stub: returns best-guess from string matching. Replace with real logic.
    """
    raw = (citation_or_court_name or "").strip()
    s = raw.lower()
    if not s:
        return {"level": "unknown", "jurisdiction": "", "binding_in": [], "persuasive_in": []}

    # COURT CODE first: the pipeline passes PA_CP_PHILA / EDPA / CA3 / SCOTUS, and the
    # prose heuristics below cannot read them ("pa_cp_phila" has no "ccp", and "pa." is
    # absent), which classified every such court as unknown.
    _cc = _code_class(raw)
    if _cc:
        _lvl, _juris = _cc
        _bind = {
            "supreme_court":      ["all_federal", "all_state"],
            "circuit_court":      ["circuit"],
            "district_court":     [],           # binds no court as precedent
            "state_supreme":      ["pa_courts"],
            "state_intermediate": ["pa_lower"],
            "state_trial":        [],
            "state_other":        [],
        }.get(_lvl, [])
        return {"level": _lvl, "jurisdiction": _juris,
                "binding_in": _bind, "persuasive_in": []}

    # SCOTUS
    if any(k in s for k in ("u.s. ", " u.s.", "supreme court", "scotus", "s.ct.", "sup ct")):
        return {"level": "supreme_court", "jurisdiction": "federal",
                "binding_in": ["all_federal", "all_state"], "persuasive_in": []}
    # Federal Circuits
    if any(k in s for k in ("3d cir", "3rd cir", "f.3d", "f. 3d", "f.4th", "circuit")):
        return {"level": "circuit_court", "jurisdiction": "federal",
                "binding_in": ["circuit"], "persuasive_in": ["other_circuits"]}
    # Federal District
    if any(k in s for k in ("e.d.pa", "e.d. pa", "edpa", "f.supp", "f. supp", "district court", "d. pa")):
        return {"level": "district_court", "jurisdiction": "federal",
                "binding_in": ["district"], "persuasive_in": ["other_districts"]}
    # PA Supreme
    if any(k in s for k in ("pa.", "pa supreme", "pennsylvania supreme")):
        return {"level": "state_supreme", "jurisdiction": "state",
                "binding_in": ["pa_courts"], "persuasive_in": []}
    # PA Superior / Commonwealth
    if any(k in s for k in ("pa. super", "pa super", "commonwealth")):
        return {"level": "state_intermediate", "jurisdiction": "state",
                "binding_in": ["pa_lower"], "persuasive_in": ["pa_courts"]}
    # PA CCP
    if any(k in s for k in ("ccp", "common pleas", "phila ccp")):
        return {"level": "state_trial", "jurisdiction": "state",
                "binding_in": ["this_case"], "persuasive_in": []}

    # USC / CFR / Statutes
    if "u.s.c" in s or "usc" in s:
        return {"level": "statute", "jurisdiction": "federal",
                "binding_in": ["all_federal"], "persuasive_in": []}
    if "c.f.r" in s or "cfr" in s:
        return {"level": "regulation", "jurisdiction": "federal",
                "binding_in": ["all_federal_subject_to_loper_bright"], "persuasive_in": []}

    return {"level": "unknown", "jurisdiction": "", "binding_in": [], "persuasive_in": []}


# ══════════════════════════════════════════════════════════════════════════════
# COURT CODES — the pipeline passes these, not prose. PA_CP_PHILA appears in 61
# call sites; before this map it classified as "unknown" and every state authority
# came back non-binding. Codes are checked BEFORE the prose heuristics below.
# ══════════════════════════════════════════════════════════════════════════════
_COURT_CODES = {
    # federal
    "SCOTUS":      ("supreme_court",     "federal"),
    "USSC":        ("supreme_court",     "federal"),
    "CA3":         ("circuit_court",     "federal"),
    "CA_OTHER":    ("circuit_court",     "federal"),
    "EDPA":        ("district_court",    "federal"),
    "ED_PA":       ("district_court",    "federal"),
    "WDPA":        ("district_court",    "federal"),
    "MDPA":        ("district_court",    "federal"),
    "DNJ":         ("district_court",    "federal"),
    "DPA_OTHER":   ("district_court",    "federal"),
    "DIST_OTHER":  ("district_court",    "federal"),
    "US_DIST":     ("district_court",    "federal"),
    # Pennsylvania. NOTE: PA_SUP means the SUPREME Court of Pennsylvania in this
    # codebase (COURT_LABELS in authority_attack_agent.py), NOT the Superior Court.
    # PA_SUPER / PA_SUPERIOR are the Superior Court. Getting these backwards would
    # silently demote the state's highest court, so both spellings are explicit.
    "PA_SUP":      ("state_supreme",      "state"),
    "PA_SUPREME":  ("state_supreme",      "state"),
    "PA_SUPER":    ("state_intermediate", "state"),
    "PA_SUPERIOR": ("state_intermediate", "state"),
    "PA_CMWLTH":   ("state_intermediate", "state"),
    "PA_COMMWLTH": ("state_intermediate", "state"),
    "PA_CP_PHILA": ("state_trial",        "state"),
    "PA_CP_OTHER": ("state_trial",        "state"),
    "PA_CP":       ("state_trial",        "state"),
    "PA_CCP":      ("state_trial",        "state"),
    "PA_STATE":    ("state_trial",        "state"),
    "STATE_OTHER": ("state_other",        "state"),
}

# What binds what, by level. Federal and state are distinct precedential systems;
# the Supremacy Clause (U.S. Const. art. VI) is the bridge for federal questions.
_BINDS_STATE_COURT = {
    "supreme_court",       # SCOTUS binds state courts on federal questions (art. VI)
    "statute",             # federal statute likewise
    "state_supreme",       # the state's own high court
    "state_intermediate",  # binds state trial courts
}
_BINDS_FEDERAL_COURT = {"supreme_court", "statute", "circuit_court"}


# One resolver for every spelling of a court the pipeline has ever passed. The rule
# contract (m1_style_profiles.json), the renderer (COURT_RULES), the gate and the finisher
# all key on the CANONICAL code; a case id ("phila_ccp:260100001"), a registry key
# ("phila_ccp"), or a renderer id ("PA_CP_PHILA") must all land on the same key or the
# rule contract is silently skipped (2026-09-27: every gate run used "phila_ccp", the *210
# contract sits under "PA_CP_PHILA", so a CCP brief missing all five mandated sections
# passed with 0 findings).
_CANONICAL_ALIASES = {
    "PA_CP_PHILA": ("PA_CP_PHILA", "PHILA_CCP", "PHILA_CP", "PHILADELPHIA_CCP", "CCP_PHILA",
                    "PA_CCP_PHILA", "PHILA"),
    "EDPA":        ("EDPA", "ED_PA", "FED", "E_D_PA", "EASTERN_DISTRICT"),
    "WDPA":        ("WDPA", "W_D_PA"),
    "MDPA":        ("MDPA", "M_D_PA"),
    "CA3":         ("CA3", "3D_CIR", "THIRD_CIRCUIT", "3RD_CIR"),
    "SCOTUS":      ("SCOTUS", "USSC", "US_SUPREME"),
    "PA_SUP":      ("PA_SUP", "PA_SUPREME"),
    "PA_SUPER":    ("PA_SUPER", "PA_SUPERIOR"),
    "PA_CMWLTH":   ("PA_CMWLTH", "PA_COMMWLTH", "PA_COMMONWEALTH"),
}
_ALIAS_LOOKUP = {a: canon for canon, aliases in _CANONICAL_ALIASES.items() for a in aliases}


def canonical_court_id(s: str) -> str:
    """Map any court spelling the pipeline uses to its canonical code, or "" if unknown.

    Accepts codes ("PA_CP_PHILA"), registry keys ("phila_ccp"), case ids
    ("phila_ccp:260100001", "fed:2:26-cv-00123") and suffixed variants ("PA_CP_PHILA_RULES").
    Unknown input returns "" -- callers must FAIL CLOSED on "", never treat it as
    "no rules apply".
    """
    if not s:
        return ""
    raw = str(s).strip()
    head = raw.split(":", 1)[0] if ":" in raw else raw
    key = re.sub(r"[^A-Z0-9_]", "", head.upper().replace(" ", "_").replace("-", "_"))
    if key in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[key]
    for alias, canon in sorted(_ALIAS_LOOKUP.items(), key=lambda kv: -len(kv[0])):
        if key.startswith(alias + "_"):
            return canon
    if key in _COURT_CODES:
        return key
    return ""


def _code_class(s: str):
    """(level, jurisdiction) when the string is a known court CODE, else None."""
    if not s:
        return None
    key = re.sub(r"[^A-Z0-9_]", "", str(s).upper().replace(" ", "_").replace("-", "_"))
    if key in _COURT_CODES:
        return _COURT_CODES[key]
    # tolerate suffixed variants like PA_CP_PHILA_RULES (3 call sites)
    for code, val in sorted(_COURT_CODES.items(), key=lambda kv: -len(kv[0])):
        if key.startswith(code + "_"):
            return val
    return None


def binding_hierarchy_for(target_code: str) -> list:
    """Codes binding on target_code, from authority_attack_agent (source of truth).

    Imported lazily so neither module hard-depends on the other. SCOTUS is added for
    state targets: BINDING_HIERARCHY lists only state appellate courts for
    PA_CP_PHILA, but a state trial court is bound by SCOTUS on federal questions.
    """
    key = re.sub(r"[^A-Z0-9_]", "", str(target_code or "").upper().replace(" ", "_"))
    try:
        from pipeline.authority_attack_agent import BINDING_HIERARCHY
    except Exception:
        try:
            from .authority_attack_agent import BINDING_HIERARCHY
        except Exception:
            BINDING_HIERARCHY = {}
    out = list(BINDING_HIERARCHY.get(key, []))
    cls = _code_class(key)
    if cls and cls[1] == "state" and "SCOTUS" not in out:
        out.append("SCOTUS")   # Supremacy Clause, federal questions
    return out


_CIRCUIT_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s*cir", re.I)
# federal district -> its governing circuit (for binding-hierarchy: only the SAME circuit binds a district)
_DISTRICT_CIRCUIT = {
    "3": ("e.d. pa", "e.d.pa", "edpa", "w.d. pa", "wdpa", "m.d. pa", "mdpa", "d. pa",
          "d.n.j", "d. n.j", "dnj", "d. del", "d.del", "ddel", "d.v.i", "dvi"),
    "2": ("s.d.n.y", "e.d.n.y", "s.d. n.y", "e.d. n.y", "sdny", "edny", "ndny", "wdny",
          "d. conn", "dconn", "d. vt", "dvt"),
    "9": ("c.d. cal", "c.d.cal", "cdcal", "n.d. cal", "ndcal", "s.d. cal", "sdcal",
          "e.d. cal", "edcal", "d. or", "d.or", "dor", "d. nev", "d.nev", "dnev",
          "w.d. wash", "wdwash", "d. ariz", "dariz", "d. haw", "dhaw", "d. alaska"),
}


def _circuit_of(s: str) -> str:
    """Best-effort federal circuit id ('3','11','dc','fed') from a citation or court string; '' if none."""
    t = (s or "").lower()
    # CODE forms first: the pipeline passes CA3 / EDPA, not "3d Cir." prose. Without
    # this, is_binding("EDPA","CA3") was False -- the Third Circuit failing to bind its
    # own district court.
    code = re.sub(r"[^a-z0-9_]", "", t.replace(" ", "_").replace("-", "_"))
    m = re.fullmatch(r"ca(\d{1,2})", code)
    if m:
        return m.group(1)
    if code in ("ca_dc", "cadc"):
        return "dc"
    if code in ("ca_fed", "cafed"):
        return "fed"
    m = _CIRCUIT_RE.search(t)
    if m:
        return m.group(1)
    if "d.c. cir" in t or "dc cir" in t:
        return "dc"
    if "fed. cir" in t or "fed cir" in t:
        return "fed"
    if "3d cir" in t or "3rd cir" in t or "third circuit" in t:
        return "3"
    for circ, pats in _DISTRICT_CIRCUIT.items():
        if any(p in t for p in pats):
            return circ
    return ""


def audit_citation(citation: str, target_court: str = "") -> dict:
    """
    Audit a citation's authority level relative to a target court. Circuit-aware:
      - SCOTUS and federal statutes bind every federal court;
      - a federal circuit binds ONLY courts within the SAME circuit (a sister circuit is persuasive);
      - a district-court opinion binds no court (persuasive only);
      - state authority handled by level.
    Returns {citation, level, binding, weight, jurisdiction, notes}.
    """
    classification = classify_court(citation)
    target_class = classify_court(target_court) if target_court else {}

    level = classification.get("level", "unknown")

    weights = {
        "supreme_court":      1.0,
        "circuit_court":      0.85,
        "district_court":     0.7,
        "state_supreme":      0.8,
        "state_intermediate": 0.65,
        "state_trial":        0.5,
        "statute":            0.95,
        "regulation":         0.6,  # below statute per Loper Bright
        "unknown":            0.0,
    }

    binding = False
    reason = ""
    if target_court and target_class:
        # ONE ENGINE (2026-10-09, E15): this block was a second hand-written copy of
        # the same rules as is_binding. Two copies is what let one of them get its
        # arguments transposed and answer False for every real authority. The reasons
        # and verdicts now come from binding_decision, same table, same direction.
        _d = binding_decision(citation, target_court)
        binding, reason = _d["binding"], _d["reason"]
        if reason == "court not resolved; failing closed":
            reason = ""          # preserve the prior quiet output for unresolved pairs

    return {
        "citation": citation,
        "level": level,
        "binding": binding,
        "weight": weights.get(level, 0.0),
        "jurisdiction": classification.get("jurisdiction", ""),
        "notes": (f"Authority: {level}; binding={binding}"
                  + (f"; {reason}" if reason else "")),
        "reason": reason,
    }


def hierarchy_violation_severity(violation_type: str = "", citation: str = "",
                                   context: dict = None) -> dict:
    """
    v17 stub — returns a uniform "moderate" severity rating for hierarchy
    violations. Placeholder until real scoring logic is built. Used by
    authority_validity which fails to import without this symbol.
    """
    return {
        "severity": "moderate",
        "score": 0.5,
        "violation_type": violation_type or "unspecified",
        "citation": citation or "",
        "rationale": "v17 stub - no fine-grained scoring yet",
    }

# Backward-compat alias for older imports (e.g. generation/scorecard_v2.py)
COURT_RANK = COURT_HIERARCHY

# BINDING_MAP: authority_validity.py imports this name, which never existed here --
# that module has been unimportable (ImportError) rather than merely unused. It maps a
# target court CODE to the codes binding on it, federal-vs-state respected, SCOTUS
# included for state targets per the Supremacy Clause.
BINDING_MAP = {
    "EDPA":        ["SCOTUS", "CA3"],
    "WDPA":        ["SCOTUS", "CA3"],
    "MDPA":        ["SCOTUS", "CA3"],
    "DNJ":         ["SCOTUS", "CA3"],
    "DPA_OTHER":   ["SCOTUS"],
    "DIST_OTHER":  ["SCOTUS"],
    "CA3":         ["SCOTUS"],
    "PA_SUP":      ["SCOTUS"],
    "PA_SUPER":    ["SCOTUS", "PA_SUP"],
    "PA_CMWLTH":   ["SCOTUS", "PA_SUP"],
    "PA_CP_PHILA": ["SCOTUS", "PA_SUP", "PA_SUPER", "PA_CMWLTH"],
    "PA_CP_OTHER": ["SCOTUS", "PA_SUP", "PA_SUPER", "PA_CMWLTH"],
    "PA_CCP":      ["SCOTUS", "PA_SUP", "PA_SUPER", "PA_CMWLTH"],
}
