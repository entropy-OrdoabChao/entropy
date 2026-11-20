"""case_profile — ONE accessor for every case-specific value the pipeline used to hardcode.

WHY THIS MODULE EXISTS
----------------------
The 2026-11-13 flow audit (output/research/FLOW_AUDIT_20261113.json, `hardcodes.must_remove`)
found 436 case-specific literals in EXECUTING code across 108 files: one matter's party name in
the signature block, one matter's ledger dates in an assert, one matter's adverse party in a
regex, one matter's docket in a slice. Every one of them is a silent cross-case contamination
bug: run the chain on a different case and the paper carries the wrong name, the wrong date or
the wrong party. The user's lock is "no hardcode, agnostic" — any pro se, any matter.

So: code asks `profile(case)` for a VALUE; it never spells one.

    from pipeline.case_profile import profile, CaseProfileGap, adverse_pattern
    p = profile("phila_ccp:260100001")
    p["filer_name"]            # -> name of record (raises CaseProfileGap if the data lacks it)
    p["signature_lines"]       # -> ["/s/ <name>", "<name>, sui juris"]
    p["adverse_names"]         # -> ["Example Insurer", "Progressive", ...]
    adverse_pattern(case)      # -> regex-escaped alternation of the adverse party's names

GAPS ARE LOUD, NEVER FILLED
---------------------------
A missing value raises CaseProfileGap naming the field and the sources consulted. There is NO
default to a real case: a silent default is how one matter's facts ended up in every matter's
paper. Use `p.get(field)` only where the caller genuinely has a no-value branch.

SOURCES (in precedence order; nothing else is read)
---------------------------------------------------
1. cases_index/case_registry.json  — canonical_id, slug, caption, judge, case_court, filed,
                                     status, case_type, docket_aliases, parties
2. cases_index/case_map.json       — case_id/docket, title, jurisdiction, case_court,
                                     all_defendants, party_tag, plaintiff, plaintiff_counsel,
                                     posture, filed, related
3. obsidian_sync/cases/<slug>/CASE_FACTS.md — party display names + roles, filer address,
                                     pro-se/sui-juris marker, adverse counsel, Control Nos.,
                                     judge (fallback)
4. obsidian_sync/cases/<slug>/CASE_TIMELINE.json (when present) — key_dates
5. cases_index/filer_identity.json (OPTIONAL) — the FILER's own constants, which are not
   case-specific at all: signature name form, capacity word ("sui juris"), address of record.
   Absent -> those fields fall back to what CASE_FACTS attests, and raise a gap if it attests
   nothing. This is the one file a new user edits to make the rig theirs.

SCHEMA — every field profile() can return
-----------------------------------------
identity      case_id, docket, slug, case_dir, jurisdiction
forum         court, judge, caption, filed, status, case_type
docket        docket_aliases[], control_numbers[]
parties       parties[{tag,name,role}], plaintiffs[], defendants[],
              adverse_role, adverse_names[], adverse_counsel[]
party tags    party_tags[], adverse_party_tags[], party_tag, note_slug,
              party_display{tag: display}, party_spellings{tag: [display, ...]}
filer         filer_role, filer_name, filer_capacity, filer_address,
              signature_name, signature_lines[], signature_block
dates         key_dates{label: iso}
meta          _sources[]  (which files actually supplied this profile)

PARTY TAGS, note_slug AND ATTESTED SPELLINGS (added 2026-11-14, hardcode_burn)
-----------------------------------------------------------------------------
The indexes already spell each matter's parties as TAGS (`case_registry.parties`,
`case_map.all_defendants`) and name the lead adverse party (`case_map.party_tag`). Code that
needed a short per-case handle, a canonical defendant label, or a "which case is this document
about" test was spelling those out instead of asking for them, e.g.

    CASE_ID_MAP   = {"equifax": "fed:2:26-cv-00123", "example_creditor": "phila_ccp:260100000", ...}
    DEFENDANT_MAP = {"capital_one": "Capital_One", "transunion": "TransUnion", ...}

so four matters were visible and the rest invisible. The three fields below carry that and
nothing more:

* `party_tags` / `adverse_party_tags` — the index's own tags, the filer's own tag removed
  (via `filer_party_tags()`), lead adverse party FIRST.
* `party_tag` — `case_map.party_tag` when the index states it, else `adverse_party_tags[0]`:
  the handle the data itself uses for this matter.
* `note_slug` — `party_tag` with its separators removed (`state_farm` -> `statefarm`): the short
  token the legacy note folders / per-case labels are keyed by. `note_slug_map()` inverts it.
* `party_spellings` / `party_display` — every CAPITALISED spelling of a tag ATTESTED in this
  case's caption + `CASE_FACTS.md` (`greensky` -> GreenSky 31x, Greensky 11x, GREEN SKY 1x) and
  the modal one. This is where `TransUnion` / `GreenSky` / `Example Card Issuer` casing comes from: the
  record attests it, no code spells it. `party_label_index()` turns it into the alias->label
  index a defendant-string normaliser needs.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import os
import re
from pathlib import Path

__all__ = [
    "filer_identity",
    "filer_address_lines",
    "filer_names",
    "filer_tokens",
    "filer_party_tags",
    "filer_surname",
    "iter_profiles",
    "CaseProfileGap",
    "CaseProfile",
    "profile",
    "adverse_pattern",
    "signature_lines",
    "resolve_slug",
    "all_case_keys",
    "case_slug_map",
    "note_slug_map",
    "party_label_index",
    "match_case_in_text",
    "clear_cache",
]


# --------------------------------------------------------------------------- roots
def _roots():
    """Candidate code roots, nearest first. A sandbox copy of this module sits under a sandbox
    root that holds only the files the sandbox changed, so each data file is resolved on its own
    (sandbox copy wins; everything else comes from the live root) — no data is ever invented."""
    env = os.environ.get("BEEZLE_ROOT")
    cands = [Path(env)] if env else []
    cands.append(Path(__file__).resolve().parents[1])
    cands += [_eroot(), Path(str(Path.home() / "beezle"))]
    seen, out = set(), []
    for c in cands:
        s = str(c)
        if s not in seen:
            seen.add(s)
            out.append(c)
    return out


def _find(rel: str):
    """First existing <root>/<rel> across the candidate roots, else None."""
    for r in _roots():
        p = r / rel
        if p.exists():
            return p
    return None


def _root() -> Path:
    p = _find("cases_index/case_registry.json")
    return p.parents[1] if p else _roots()[0]


def _vault(root: Path) -> Path:
    env = os.environ.get("BEEZLE_VAULT")
    if env:
        return Path(env)
    for c in (root / "obsidian_sync", (_eroot() / "obsidian_sync")):
        if (c / "cases").is_dir():
            return c
    return root / "obsidian_sync"


# --------------------------------------------------------------------------- errors
class CaseProfileGap(KeyError):
    """A case-specific value the data does not supply. Never fill it — fix the data."""


class CaseProfile(dict):
    """dict whose __getitem__ raises CaseProfileGap (naming the field + sources) instead of
    handing back a default. `.get()` behaves normally for genuinely optional reads."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

    def __getitem__(self, field):
        if field in self:
            v = super().__getitem__(field)
            if v not in (None, "", [], {}):
                return v
        raise CaseProfileGap(
            "case_profile GAP: field %r is not supplied for case %r — sources consulted: %s. "
            "Fix the source data (case_registry/case_map/CASE_FACTS); do not hardcode a value."
            % (field, super().get("case_id") or "?", ", ".join(super().get("_sources") or ["<none>"]))
        )

    def require(self, *fields):
        """Fetch several fields at once; the first gap raises."""
        return [self[f] for f in fields]


# --------------------------------------------------------------------------- loaders
_CACHE: dict = {}


def clear_cache():
    _CACHE.clear()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _indexes():
    """(registry, case_map, key -> (canonical_key, reg_entry, map_entry)) built from the data."""
    if "idx" in _CACHE:
        return _CACHE["idx"]
    rp = _find("cases_index/case_registry.json")
    mp = _find("cases_index/case_map.json")
    reg = _load_json(rp) if rp else {}
    cmap = _load_json(mp) if mp else {}
    reg = reg if isinstance(reg, dict) else {}
    cmap = cmap if isinstance(cmap, dict) else {}

    def digits(s):
        return re.sub(r"\D", "", str(s or ""))

    # group entries by a join key: the bare docket digits, else the case_id string
    groups = {}

    def join_key(entry, fallback):
        cid = str(entry.get("case_id") or entry.get("docket") or "").strip()
        d = digits(cid)
        if cid and not d:
            return cid.lower()
        if re.fullmatch(r"\d{9}", d):           # Phila CCP 9-digit docket
            return d
        return (cid or fallback).lower()

    for k, v in reg.items():
        if not isinstance(v, dict):
            continue
        cid = str(v.get("canonical_id") or k)
        bare = cid.split(":", 1)[1] if ":" in cid else cid
        g = groups.setdefault(join_key({"case_id": bare}, cid), {})
        g.setdefault("reg_keys", []).append(k)
        g["reg"] = v
        g.setdefault("canonical", cid)
    for k, v in cmap.items():
        if not isinstance(v, dict):
            continue
        jk = join_key(v, k)
        g = groups.setdefault(jk, {})
        g.setdefault("map_keys", []).append(k)
        g["map"] = v
        if "canonical" not in g:
            jur = str(v.get("jurisdiction") or "").strip().lower()
            cid = str(v.get("case_id") or v.get("docket") or k).strip()
            g["canonical"] = f"{jur}:{cid}" if jur and jur not in ("pa", "tbd") and ":" not in cid else (
                f"phila_ccp:{cid}" if re.fullmatch(r"\d{9}", cid) else cid)

    # alias -> group
    alias = {}

    def put(a, g):
        a = str(a or "").strip().lower()
        if a:
            alias.setdefault(a, g)

    for jk, g in groups.items():
        reg_e, map_e = g.get("reg") or {}, g.get("map") or {}
        put(jk, g)
        put(g.get("canonical"), g)
        for k in g.get("reg_keys", []) + g.get("map_keys", []):
            put(k, g)
        for e in (reg_e, map_e):
            put(e.get("canonical_id"), g)
            put(e.get("case_id"), g)
            put(e.get("docket"), g)
            put(e.get("slug"), g)
            for a in e.get("docket_aliases") or []:
                put(a, g)
        cid = str(reg_e.get("case_id") or map_e.get("case_id") or map_e.get("docket") or "")
        if re.fullmatch(r"\d{9}", re.sub(r"\D", "", cid)):
            put("phila_ccp:" + re.sub(r"\D", "", cid), g)
        if ":" in str(g.get("canonical") or ""):
            put(str(g["canonical"]).split(":", 1)[1], g)
        fp = reg_e.get("folder_path") or ""
        if fp:
            put(str(fp).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1], g)
    _CACHE["idx"] = (reg, cmap, alias, groups)
    return _CACHE["idx"]


def all_case_keys():
    """Every alias the resolver answers to — the lint uses this to derive its literal set."""
    _, _, alias, _ = _indexes()
    return sorted(alias)


def resolve_slug(case_id_or_slug) -> str:
    return profile(case_id_or_slug).get("slug") or ""


# --------------------------------------------------------------------------- CASE_FACTS
_ROLES = ("plaintiff", "defendant", "appellant", "appellee", "petitioner", "respondent",
          "movant", "garnishee", "intervenor")
_ROLE_RX = "|".join(_ROLES)
_CORP_STOP = {"insurance", "ins", "company", "co", "llc", "inc", "corp", "corporation", "lp",
              "llp", "pc", "plc", "na", "mortgage", "lending", "advanced", "services", "group",
              "bank", "trust", "financial", "home", "the", "and"}
_NAME_CHARS = r"[A-Z][A-Za-z.\u2019'\-]*"
# heads too generic to stand alone as a party name
_GENERIC_HEAD = {"state", "united", "first", "national", "american", "general", "city",
                 "county", "court", "commonwealth", "department", "office", "board", "new"}


def _norm_tag(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def _tag_display(tag) -> str:
    return " ".join(w.capitalize() for w in str(tag or "").split("_") if w)


def _short_forms(name: str):
    """'Example Insurer' -> {'Progressive'}; 'State Farm Fire and
    Casualty' -> {'State Farm'}. The distinctive head of the name, stopping at a corporate or
    generic word. Pure derivation — no party list anywhere."""
    out = set()
    toks = [t for t in re.split(r"[\s,]+", name.strip()) if t]
    head = []
    for t in toks:
        if _norm_tag(t) in _CORP_STOP and head:
            break
        if not re.match(r"^[A-Z]", t):
            break
        head.append(t.strip(".,"))
        if len(head) >= 3:
            break
    if head and len(" ".join(head)) >= 4:
        out.add(" ".join(head))
    # a lone first token is kept only when it is distinctive: the whole name is one word, or the
    # token is long and not a generic geographic/legal word ('STATE' would match 'State of PA').
    if head and _norm_tag(head[0]) not in _CORP_STOP | _GENERIC_HEAD and (
            len(head) == 1 and len(head[0]) >= 4 or len(head[0]) >= 6):
        out.add(head[0])
    return {o for o in out if len(o) >= 4}


_SQUASH_RX = re.compile(r"[^a-z0-9]+")
# corporate suffixes a RAW party string may carry that the index tag does not ('avant' is the
# tag; 'Avant, Inc.' is the string). English furniture, not case data.
_SUFFIX_WORDS = ("inc", "llc", "corp", "co", "llp", "lp", "pc", "na", "ltd", "plc",
                 "incorporated", "corporation", "company", "services", "solutions")


def _squash(s) -> str:
    return _SQUASH_RX.sub("", str(s or "").lower())


def _attested_spellings(text: str, tags) -> dict:
    """tag -> {display spelling: count} for every CAPITALISED spelling of the tag the TEXT
    attests: 'greensky' finds GreenSky / Greensky / GREEN SKY, 'capital_one' finds Example Card Issuer.
    Matching is by SQUASHED letters, so the tag's separators need not agree with the record's
    ('trans_union', 'TransUnion' and 'Trans Union' are one party). No name is spelled here —
    the record supplies the spelling, this only counts it."""
    out = {t: {} for t in tags}
    want = {}
    for t in tags:
        sq = _squash(t)
        if sq:
            want.setdefault(sq, t)
    if not want or not text:
        return out
    maxw = max(len(str(t).split("_")) for t in tags) + 1
    toks = re.findall(r"[A-Za-z0-9&'\u2019.\-]+", text)
    for i in range(len(toks)):
        for n in range(1, maxw + 1):
            if i + n > len(toks):
                break
            phrase = " ".join(toks[i:i + n])
            t = want.get(_squash(phrase))
            if t and re.search(r"[A-Z]", phrase):
                out[t][phrase] = out[t].get(phrase, 0) + 1
    return out


def _modal_spelling(counts: dict, tag: str) -> str:
    """The spelling the record attests MOST, preferring a mixed-case form over ALL CAPS: a
    caption shouts every name, the body spells it the way the party itself does."""
    if not counts:
        return _tag_display(tag)

    def rank(item):
        s, n = item
        shouted = 0 if (s != s.upper() and s != s.lower()) else 1
        return (shouted, -n, -len(s), s)

    return sorted(counts.items(), key=rank)[0][0]


def _parse_case_facts(text: str) -> dict:
    """Role -> display names, filer address, counsel, control numbers, pro-se markers.
    Every pattern family below is a FORM the vault actually uses; none names a party."""
    out = {"roles": {}, "addresses": {}, "pro_se_roles": set(), "counsel": [], "controls": [],
           "judge": None}

    # (A) '### Plaintiff' block with '- **Name**: X' / '- **Address**: Y' / '- **Status**: Pro Se'
    for m in re.finditer(r"^#{2,4}\s*(%s)s?\s*$(.*?)(?=^#{2,4}\s|\Z)" % _ROLE_RX,
                         text, re.M | re.S | re.I):
        role, body = m.group(1).lower(), m.group(2)
        nm = re.search(r"^\s*[-*]?\s*\*\*Name\*\*\s*:\s*(.+)$", body, re.M)
        if nm:
            out["roles"].setdefault(role, []).append(nm.group(1).strip())
        ad = re.search(r"^\s*[-*]?\s*\*\*Address\*\*\s*:\s*(.+)$", body, re.M)
        if ad:
            out["addresses"].setdefault(role, ad.group(1).strip())
        if re.search(r"\b(pro se|sui juris|self-represented)\b", body, re.I):
            out["pro_se_roles"].add(role)
        for c in re.finditer(r"\*\*(.+?)\*\*\s*[-\u2014]\s*Represented by\s+([^\n]+)", body):
            out["counsel"].append(c.group(2).strip())

    # (B) inline '**Plaintiff:** X' / '- **Plaintiff**: X (pro se)'
    for m in re.finditer(r"^\s*[-*]?\s*\*\*(%s)s?(?:\*\*)?\s*:?\s*(?:\*\*)?\s*[:\u2014-]?\s*"
                         r"([^\n*]{3,90})$" % _ROLE_RX, text, re.M | re.I):
        role, val = m.group(1).lower(), m.group(2).strip(" :\u2014-")
        nm = re.match(r"^((?:%s)(?:\s+(?:%s)){0,5})" % (_NAME_CHARS, _NAME_CHARS), val)
        if nm:
            cand = nm.group(1).strip()
            if len(cand) >= 4:
                out["roles"].setdefault(role, []).append(cand)
        if re.search(r"\((?:pro se|sui juris|self-represented)\)", val, re.I):
            out["pro_se_roles"].add(role)

    # (C) QUOTED prose only -- '- Parties: ... Defendant "FILER, FILER" at ...'. Unquoted prose
    # was tried and rejected: it pulled CROSS-CASE parties (a sister case's defendant named in a
    # posture paragraph) into this case's party list, which is the very bug this module exists for.
    for m in re.finditer(r"\b(%s)s?\s+[\"\u201c]([^\"\u201d\n]{3,60})[\"\u201d]" % _ROLE_RX, text):
        role, cand = m.group(1).lower(), re.sub(r"\s+", " ", m.group(2)).strip(" ,")
        if len(cand) >= 4 and _norm_tag(cand) not in _CORP_STOP:
            out["roles"].setdefault(role, []).append(cand)

    for m in re.finditer(r"Control\s*(?:No\.?|Number)\s*\"?(\d{7,9})", text, re.I):
        out["controls"].append(m.group(1))
    for m in re.finditer(r"counsel\s+([A-Z][\w&.,'\- ]{3,60}?(?:LLP|LLC|PC|P\.C\.|LTD|Group))", text):
        out["counsel"].append(m.group(1).strip())
    for m in re.finditer(r"Represented by\s+([^\n.;]{3,70})", text):
        out["counsel"].append(m.group(1).strip())
    jm = re.search(r"^\s*[-*]?\s*\*\*Judge\*\*\s*:\s*(.+)$", text, re.M) or \
        re.search(r"^#{2,4}\s*Judge\s*:\s*(.+)$", text, re.M)
    if jm:
        out["judge"] = jm.group(1).strip()
    # address of record, stated as a bare phrase in posture prose
    am = re.search(r"address of record[^\n]*?(\d{2,6}\s+[A-Z][\w.\- ]{2,40}(?:\s+(?:St|Street|Ave|"
                   r"Avenue|Rd|Road|Blvd|Pkwy|Dr|Drive)\b)[^\n,;)]*)", text)
    if am:
        out["addresses"].setdefault("_of_record", am.group(1).strip())
    return out


# --------------------------------------------------------------------------- build
def _own_tags(p: dict, tag_order) -> set:
    """Which of these tags are the FILER's own side.

    `filer_party_tags()` is the designed source, but it reads cases_index/filer_identity.json,
    which is currently ABSENT (see the sandbox REPORT's hooks_needed) — so it answers the empty
    set, and the filer would end up listed among its own adversaries. This falls back to the
    case's OWN record: the filer / plaintiff names this profile already resolved."""
    own = set(filer_party_tags())
    toks = set()
    for n in ([p.get("filer_name"), p.get("signature_name")]
              + list(p.get("plaintiffs") or [])):
        for t in re.split(r"[^A-Za-z]+", str(n or "")):
            if len(t) >= 4:
                toks.add(t.lower())
    for t in tag_order:
        sq = _squash(t)
        if sq and sq in toks:
            own.add(t)
    return own


def profile(case_id_or_slug, root=None) -> CaseProfile:
    """Return the CaseProfile for any id/slug/docket/alias the data knows."""
    key = str(case_id_or_slug or "").strip()
    if not key:
        raise CaseProfileGap("case_profile: empty case id")
    ck = key.lower()
    if ck in _CACHE.setdefault("prof", {}):
        return _CACHE["prof"][ck]

    _, _, alias, _groups = _indexes()
    g = alias.get(ck) or alias.get(re.sub(r"\D", "", ck)) or alias.get(ck.replace("_", "-"))
    if g is None:
        g = alias.get(ck.split(":")[-1])
    if g is None:
        raise CaseProfileGap(
            "case_profile: case %r is in no index (cases_index/case_registry.json, "
            "cases_index/case_map.json). Register the case; never hardcode its values." % key)

    rt = Path(root) if root else _root()
    reg_e, map_e = g.get("reg") or {}, g.get("map") or {}
    sources = []
    if reg_e:
        sources.append("cases_index/case_registry.json")
    if map_e:
        sources.append("cases_index/case_map.json")

    p = {}
    p["case_id"] = str(reg_e.get("canonical_id") or g.get("canonical") or key)
    docket = str(reg_e.get("case_id") or map_e.get("case_id") or map_e.get("docket") or "").strip()
    p["docket"] = docket or None
    p["jurisdiction"] = reg_e.get("jurisdiction") or map_e.get("jurisdiction") or (
        p["case_id"].split(":")[0] if ":" in p["case_id"] else None)
    p["court"] = reg_e.get("case_court") or map_e.get("case_court") or map_e.get("court") or None
    p["judge"] = reg_e.get("judge") or map_e.get("judge") or None
    p["caption"] = reg_e.get("caption") or map_e.get("title") or None
    p["filed"] = reg_e.get("filed") or map_e.get("filed") or None
    p["status"] = reg_e.get("status") or map_e.get("status") or map_e.get("posture") or None
    p["case_type"] = reg_e.get("case_type") or map_e.get("type") or None
    p["docket_aliases"] = list(reg_e.get("docket_aliases") or ([docket] if docket else []))

    # ---- slug + case dir
    slug = reg_e.get("slug") or None
    if not slug:
        fp = str(reg_e.get("folder_path") or "")
        if fp:
            slug = fp.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        for k in g.get("map_keys", []) + g.get("reg_keys", []):
            if (_vault(rt) / "cases" / k).is_dir():
                slug = k
                break
    p["slug"] = slug
    case_dir = (_vault(rt) / "cases" / slug) if slug else None
    p["case_dir"] = str(case_dir) if case_dir and case_dir.is_dir() else None

    # ---- CASE_FACTS.md
    cf = {}
    cf_text = ""
    if case_dir and (case_dir / "CASE_FACTS.md").exists():
        cf_text = (case_dir / "CASE_FACTS.md").read_text(encoding="utf-8", errors="replace")
        cf = _parse_case_facts(cf_text)
        sources.append(f"obsidian_sync/cases/{slug}/CASE_FACTS.md")
        p["judge"] = p["judge"] or cf.get("judge")

    # ---- parties: tags from the indexes, display names from the caption + CASE_FACTS
    def_tags = [_norm_tag(t) for t in (map_e.get("all_defendants") or []) if t]
    party_tags = [_norm_tag(t) for t in (reg_e.get("parties") or []) if t]
    cap = p["caption"] or ""
    left, right = "", ""
    cs = re.split(r"\s+(?:VS\.?|V\.?)\s+", cap, maxsplit=1, flags=re.I)
    if len(cs) == 2:
        left, right = cs[0].strip(), re.sub(r"\s+ET\s*AL\.?$", "", cs[1].strip(), flags=re.I)

    cf_roles = {r: [n for n in dict.fromkeys(v)] for r, v in (cf.get("roles") or {}).items()}

    def role_names(role):
        return list(dict.fromkeys(cf_roles.get(role) or []))

    # the FILER's role: explicit posture > pro-se marker in CASE_FACTS > caption side not
    # covered by the defendant tags. Undecidable -> gap (never guessed).
    filer_role = None
    role_src = None
    posture = str(map_e.get("posture") or "")
    pm = re.match(r"\s*(%s)\b" % _ROLE_RX, posture, re.I)
    if pm:
        filer_role, role_src = pm.group(1).lower(), "case_map.posture"
    if not filer_role and len(cf.get("pro_se_roles") or ()) == 1:
        filer_role, role_src = next(iter(cf["pro_se_roles"])), "CASE_FACTS pro-se marker"
    if not filer_role and map_e.get("plaintiff") and left:
        # the index names the PLAINTIFF explicitly. If that name is the caption's LEFT side,
        # the plaintiff is the adverse party and the filer is the defendant.
        pl, lt = _norm_tag(map_e["plaintiff"]), _norm_tag(left)
        head = pl.split("_")[0]
        if head and (lt.startswith(head) or pl.startswith(lt.split("_")[0])):
            filer_role, role_src = "defendant", "case_map.plaintiff is the caption's left side"
    if not filer_role and left:
        lt = _norm_tag(left.split(",")[0].split()[0] if left.split() else left)
        if lt and not any(t.startswith(lt) or lt.startswith(t.split("_")[0]) for t in def_tags):
            filer_role, role_src = "plaintiff", "caption left side is not a defendant tag"
    p["filer_role"] = (filer_role or "").capitalize() or None
    p["_filer_role_source"] = role_src
    adverse_role = {"plaintiff": "defendant", "defendant": "plaintiff",
                    "appellant": "appellee", "appellee": "appellant",
                    "petitioner": "respondent", "respondent": "petitioner"}.get(filer_role or "")
    p["adverse_role"] = (adverse_role or "").capitalize() or None

    # ---- adverse display names: tags + caption side + CASE_FACTS role names, plus short forms
    adv = []
    if map_e.get("plaintiff") and filer_role == "defendant":
        adv.append(str(map_e["plaintiff"]).strip())
    adv += role_names(adverse_role or "")
    if filer_role == "plaintiff":
        adv += [_tag_display(t) for t in def_tags]
        if right:
            adv.append(right)
    elif filer_role == "defendant" and left:
        adv.append(left)
    adv = [re.sub(r"\s+", " ", a).strip(" ,.") for a in adv if a and len(a) >= 3]
    names = set()
    for a in adv:
        names.add(a)
        names |= _short_forms(a)
    # a one-word tag display ('Example Creditor') is itself the short form
    p["adverse_names"] = sorted({n for n in names if len(n) >= 4}, key=lambda s: (-len(s), s))

    # ---- filer name / address / signature
    filer_names = role_names(filer_role or "")
    # prefer the longest attested form (full middle name over an initial) for the name of record
    filer_name = max(filer_names, key=len) if filer_names else None
    if not filer_name and filer_role == "plaintiff" and left:
        filer_name = left
    if not filer_name and filer_role == "defendant" and right:
        filer_name = right
    addr = (cf.get("addresses") or {}).get(filer_role or "") or \
        (cf.get("addresses") or {}).get("_of_record")

    ip = _find("cases_index/filer_identity.json")
    ident = _load_json(ip) if ip else {}
    if ident:
        sources.append("cases_index/filer_identity.json")
    p["filer_name"] = filer_name or None
    p["filer_capacity"] = ident.get("capacity") or None
    p["filer_address"] = ident.get("address") or addr or None
    p["signature_name"] = ident.get("signature_name") or filer_name or None
    p["filer_aliases"] = sorted({a for a in ([p["signature_name"]] + filer_names +
                                             list(ident.get("name_aliases") or [])) if a})

    # ---- party TAGS as the indexes spell them, the filer's own tag removed, lead adverse first
    tag_order = []
    for t in (([map_e["party_tag"]] if map_e.get("party_tag") else []) +
              list(map_e.get("all_defendants") or []) + list(reg_e.get("parties") or [])):
        nt = _norm_tag(t)
        if nt and nt not in tag_order:
            tag_order.append(nt)
    own_tags = _own_tags(p, tag_order)
    p["party_tags"] = tag_order
    adv_tags = [t for t in tag_order if t not in own_tags]
    p["adverse_party_tags"] = adv_tags
    lead = _norm_tag(map_e.get("party_tag")) or (adv_tags[0] if adv_tags else "")
    p["party_tag"] = lead or None
    p["note_slug"] = _squash(lead) or None
    counts = _attested_spellings("\n".join([cap, cf_text]), tag_order)
    p["party_spellings"] = {t: [s for s, _ in sorted(counts[t].items(),
                                                     key=lambda kv: (-kv[1], kv[0]))]
                            for t in tag_order}
    p["party_display"] = {t: _modal_spelling(counts[t], t) for t in tag_order}

    sig = []
    if p["signature_name"]:
        sig.append("/s/ " + p["signature_name"])
        sig.append(p["signature_name"] + (", " + p["filer_capacity"] if p["filer_capacity"] else ""))
    p["signature_lines"] = sig
    p["signature_block"] = "\n".join(sig) if sig else None

    # ---- counsel + controls + dates
    p["adverse_counsel"] = sorted({c for c in (list(cf.get("counsel") or []) +
                                               ([map_e["plaintiff_counsel"]] if map_e.get("plaintiff_counsel") else []))
                                   if c})
    ctrl = list(dict.fromkeys([str(c) for c in (cf.get("controls") or [])] +
                              [str(c) for c in (map_e.get("control_numbers") or [])]))
    p["control_numbers"] = ctrl
    kd = {}
    if p["filed"]:
        kd["filed"] = p["filed"]
    tl = (case_dir / "CASE_TIMELINE.json") if case_dir else None
    if tl and tl.exists():
        sources.append(f"obsidian_sync/cases/{slug}/CASE_TIMELINE.json")
        data = _load_json(tl)
        rows = data if isinstance(data, list) else (data.get("events") or data.get("timeline") or [])
        for ev in rows if isinstance(rows, list) else []:
            if isinstance(ev, dict):
                d = ev.get("date") or ev.get("iso") or ev.get("when")
                lab = ev.get("event") or ev.get("label") or ev.get("title") or ev.get("what")
                if d and lab:
                    kd.setdefault(str(lab)[:80], str(d))
    p["key_dates"] = kd
    p["_sources"] = sources

    prof = CaseProfile({k: v for k, v in p.items()})
    _CACHE["prof"][ck] = prof
    return prof


# --------------------------------------------------------------------------- helpers
def adverse_pattern(case_id_or_slug, group: str = "") -> str:
    """Regex alternation of the adverse party's names, longest first, every name re.escape'd.
    Replaces hardcoded '(Progressive|Defendant)' style regexes. `group` is appended inside the
    alternation (e.g. 'Defendant|Plaintiff') for the role words the rule itself uses."""
    names = list(profile(case_id_or_slug)["adverse_names"])
    alts = [re.escape(n) for n in names]
    if group:
        alts += [g for g in group.split("|") if g]
    return "(?:" + "|".join(alts) + ")" if alts else "(?!)"


def signature_lines(case_id_or_slug) -> list:
    return list(profile(case_id_or_slug)["signature_lines"])


# ---- FILER-level helpers (no case needed): the filer is the same person in every matter, so
# these answer "is this party us?" without any code spelling a name.
def filer_names() -> list:
    """Every attested form of the FILER's own name (signature name + aliases), longest first.
    Source: cases_index/filer_identity.json. Empty list when the rig has no identity file."""
    ip = _find("cases_index/filer_identity.json")
    ident = _load_json(ip) if ip else {}
    names = [ident.get("signature_name")] + list(ident.get("name_aliases") or [])
    return sorted({re.sub(r"\s+", " ", str(n)).strip() for n in names if n}, key=lambda s: -len(s))


def filer_identity() -> dict:
    """The raw filer identity record (cases_index/filer_identity.json), {} when absent."""
    ip = _find("cases_index/filer_identity.json")
    return _load_json(ip) if ip else {}


def filer_address_lines() -> list:
    """The address of record as the FILINGS set it, one list item per rendered line."""
    ident = filer_identity()
    lines = list(ident.get("address_lines") or [])
    if not lines and ident.get("address"):
        parts = [p.strip() for p in str(ident["address"]).split(",")]
        if len(parts) >= 3:
            lines = [", ".join(parts[:-2]), ", ".join(parts[-2:])]
        else:
            lines = [str(ident["address"])]
    return lines


def filer_tokens() -> set:
    """Lowercase surname/name tokens of the filer — the 'is this doc ours?' test. Derived from
    filer_names(); a hyphenated given name contributes each half."""
    out = set()
    for n in filer_names():
        for t in re.split(r"[^A-Za-z]+", n):
            if len(t) >= 4:
                out.add(t.lower())
    return out


def filer_party_tags() -> set:
    """The filer's own party tag(s) as the indexes spell them (e.g. {'filer'}), so a party list
    can drop 'us' without naming us. Derived: the filer name tokens that appear as a party tag."""
    _, _, _, groups = _indexes()
    tags = set()
    for g in groups.values():
        for t in (g.get("reg") or {}).get("parties") or []:
            tags.add(_norm_tag(t))
        for t in (g.get("map") or {}).get("all_defendants") or []:
            tags.add(_norm_tag(t))
    toks = filer_tokens()
    return {t for t in tags if t and (t in toks or any(p in toks for p in t.split("_")))}


def filer_surname() -> str:
    """The filer's surname for 'X v. Y' title construction. Taken from signature_name (the name
    of record), NOT from the longest alias: an alias may be in surname-first docket form
    ('FILER, FILER-FARDY BABAH'), whose last token is a given name."""
    ident = filer_identity()
    nm = str(ident.get("signature_name") or "").strip()
    if not nm:
        ns = filer_names()
        nm = ns[0] if ns else ""
    if "," in nm:                      # surname-first form -> the part before the comma
        return nm.split(",")[0].strip().split()[-1] if nm.split(",")[0].strip() else ""
    toks = nm.split()
    return toks[-1] if toks else ""


def iter_profiles():
    """Yield one CaseProfile per registered case (deduped by case_id) — for whole-portfolio
    reports that used to carry a hand-written 5-row table of cases."""
    _, _, _, groups = _indexes()
    seen = set()
    for g in groups.values():
        key = (g.get("canonical") or (g.get("reg_keys") or g.get("map_keys") or [None])[0])
        if not key:
            continue
        try:
            pr = profile(key)
        except CaseProfileGap:
            continue
        cid = pr.get("case_id")
        if cid and cid not in seen:
            seen.add(cid)
            yield pr


# ---- PORTFOLIO maps: what a hand-written {case: slug} table used to be -----------------------
def case_slug_map(require_subdir=None) -> dict:
    """{case_id: vault slug} for every registered case that HAS a vault folder — optionally only
    those whose folder holds `require_subdir` (e.g. 'drafts'). Replaces the 4-row CASES /
    SLUG_FROM_CASE tables: a registered case was invisible to those tools until someone edited
    the table, and a case the table named but the vault lacked produced a dead path."""
    out = {}
    for pr in iter_profiles():
        cid, slug, cdir = pr.get("case_id"), pr.get("slug"), pr.get("case_dir")
        if not (cid and slug and cdir):
            continue
        if require_subdir and not (Path(cdir) / str(require_subdir)).is_dir():
            continue
        out.setdefault(cid, slug)
    return out


def note_slug_map() -> dict:
    """{note_slug: case_id} — the inverse of profile()['note_slug'], for the short per-case
    handle the document-note folders and per-case labels are keyed by."""
    out = {}
    for pr in iter_profiles():
        ns, cid = pr.get("note_slug"), pr.get("case_id")
        if ns and cid:
            out.setdefault(ns, cid)
    return out


def party_label_index():
    """(keys, order) for turning a RAW party string into a canonical party LABEL.

        keys  : normalised alias -> label   ('trans_union' -> 'TransUnion')
        order : [(needle, label), ...] longest needle first, for a substring fallback

    The label is the record's own modal spelling with spaces -> underscores, which is how the
    Obsidian party pages are named ('Example Card Issuer' -> 'Capital_One'). Aliases come from the tag,
    the squashed tag, every spelling the record attests, the tag plus a corporate suffix, and
    the long adverse-party names the indexes carry — so no code spells a party."""
    keys, order = {}, {}
    own = filer_party_tags()
    for pr in iter_profiles():
        disp = pr.get("party_display") or {}
        spell = pr.get("party_spellings") or {}
        for tag in pr.get("adverse_party_tags") or []:
            if tag in own:
                continue
            label = re.sub(r"\s+", "_", (disp.get(tag) or _tag_display(tag)).strip()).strip("_")
            if not label:
                continue
            sq = _squash(tag)
            cand = {tag, sq} | {_norm_tag(s) for s in spell.get(tag) or []}
            cand |= {f"{tag}_{sfx}" for sfx in _SUFFIX_WORDS}
            cand |= {f"{sq}_{sfx}" for sfx in _SUFFIX_WORDS}
            if len(sq) >= 4:
                for n in pr.get("adverse_names") or []:
                    if _squash(n).startswith(sq):
                        cand.add(_norm_tag(n))
            for k in cand:
                if k:
                    keys.setdefault(k, label)
            needles = {tag, sq}
            # the record's own spellings are needles too, so a party string that only CONTAINS
            # the name still normalises ('trans_union_llc' -> TransUnion via 'trans_union')
            needles |= {_norm_tag(s) for s in spell.get(tag) or []}
            head = tag.split("_")[0]
            if len(head) >= 5 and head not in _GENERIC_HEAD and head not in _CORP_STOP:
                needles.add(head)
            for n in needles:
                if n and len(n) >= 5 and n not in _GENERIC_HEAD and n not in _CORP_STOP:
                    order.setdefault(n, label)
    return keys, sorted(order.items(), key=lambda kv: (-len(kv[0]), kv[0]))


def match_case_in_text(text):
    """The ONE registered case a path (or any blob) identifies, by that case's OWN identifiers.
    Two passes: STRONG identifiers first (docket + aliases + bare docket digits, vault slug,
    note slug, lead party tag), then WEAK ones (the other adverse party tags and their attested
    spellings). Returns None when nothing matches OR when the pass is ambiguous.

    This replaces `if "<docket>" in path or "<party>" in path: return "<case>"` ladders. Those
    ladders answered with the FIRST literal that hit, so a party shared by two matters (a
    garnishee named in one case and sued in another) filed one matter's documents under the
    other. Ambiguous now means None, which is the honest answer."""
    low = str(text or "").lower()
    if not low:
        return None
    strong, weak = {}, {}
    for pr in iter_profiles():
        cid = pr.get("case_id")
        if not cid:
            continue
        s, w = set(), set()
        for d in (list(pr.get("docket_aliases") or []) + [pr.get("docket")]):
            if d:
                s.add(str(d).lower())
                dd = re.sub(r"\D", "", str(d))
                if len(dd) >= 7:
                    s.add(dd)
        for v in (pr.get("slug"), pr.get("note_slug"), pr.get("party_tag")):
            if v:
                v = str(v).lower()
                s |= {v, v.replace("_", "-"), v.replace("_", " ")}
        for tag in pr.get("adverse_party_tags") or []:
            for v in {tag, _squash(tag), tag.replace("_", "-"), tag.replace("_", " ")}:
                if len(v) >= 5:
                    w.add(v.lower())
        for name in pr.get("party_spellings") or {}:
            for sp in (pr.get("party_spellings") or {}).get(name) or []:
                if len(sp) >= 5:
                    w.add(sp.lower())
        strong[cid] = {n for n in s if len(n) >= 5}
        weak[cid] = w - strong[cid]
    for bucket in (strong, weak):
        hits = {cid for cid, needles in bucket.items() if any(n in low for n in needles)}
        if len(hits) == 1:
            return next(iter(hits))
        if hits:
            return None
    return None


if __name__ == "__main__":  # pragma: no cover - manual proof
    import sys
    args = sys.argv[1:] or all_case_keys()
    for a in args:
        try:
            pr = profile(a)
        except CaseProfileGap as e:
            print(f"{a}: GAP {e}")
            continue
        print("=" * 70)
        print(a, "->", pr.get("case_id"), "|", pr.get("slug"))
        for f in ("docket", "court", "judge", "caption", "filer_role", "_filer_role_source",
                  "filer_name", "signature_name", "filer_capacity", "filer_address",
                  "adverse_role", "adverse_names", "adverse_counsel", "control_numbers",
                  "signature_lines", "_sources"):
            print(f"  {f:22s} {pr.get(f)!r}")
