"""
generation/md_to_docx.py - v2 block-stitcher renderer (FINAL)

Reads:
  obsidian_sync/templates/filing_types/<filing_type>.yaml  (block order + when conditions)
  obsidian_sync/templates/blocks/<category>/<block>.md     (block content with frontmatter)

Renders:
  Court-aware .docx with proper caption, numbered paragraphs (manual `1.` style only),
  centered bold section headers, footer page numbers, signature / cert-of-service.

Court-agnostic: EDPA, 3D_CIR, SCOTUS, PA_CP_PHILA, PA_SUPER, PA_COMMWLTH, PA_SUPREME.

Backward compatible: legacy `render_md_to_docx(md_text, out_path, court_id)` preserved.
"""
from __future__ import annotations
import re
import yaml
from pathlib import Path
from typing import Optional, Any
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VAULT = PROJECT_ROOT / "obsidian_sync"
TEMPLATES_DIR = VAULT / "templates"
BLOCKS_DIR = TEMPLATES_DIR / "blocks"
FILING_TYPES_DIR = TEMPLATES_DIR / "filing_types"

# === OMNIBUS_FIX_V1: strip HTML comments at every render entry point ===
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
def _strip_html_comments(text):
    """Remove <!-- ... --> blocks. Defense at every render entry path."""
    if not text:
        return text
    return _HTML_COMMENT_RE.sub("", text)


COURT_ALIASES = {
    # Names callers actually pass, mapped to the ONE key COURT_RULES holds. m1's own gate
    # normalizes "PA_SUPERIOR" -> "PA_SUPER"; this renderer did not, so the .get() fell
    # through to EDPA and stamped "UNITED STATES DISTRICT COURT / FOR THE EASTERN DISTRICT
    # OF PENNSYLVANIA" on a Pa.R.A.P. 123 application to the Superior Court -- with the
    # correct 0000 EDA 2026 docket beside it. Wrong court, right number, gate PASS.
    "PA_SUPERIOR": "PA_SUPER",
    "SUPERIOR": "PA_SUPER",
    "PA_SUPERIOR_COURT": "PA_SUPER",
    "PA_COMMONWEALTH": "PA_COMMWLTH",
    "PA_SUPREME_COURT": "PA_SUPREME",
    "PHILA_CCP": "PA_CP_PHILA",
    "PA_CP": "PA_CP_PHILA",
    "EDPA_DISTRICT": "EDPA",
    "3DCIR": "3D_CIR",
    "THIRD_CIRCUIT": "3D_CIR",
}


def _court_rules_or_die(court_id):
    """Resolve a court key, or REFUSE. Never silently default to a federal caption.

    A caption naming the wrong court is not a cosmetic defect: it is the first thing the
    prothonotary reads, and it invites rejection of a paper whose entire complaint is that
    the offices below have not acted. Defaulting made that failure invisible -- every gate
    passed, because the document was a well-formed EDPA document. Refusal is the only safe
    mode: an unknown court id is a caller bug, and the caller must name the forum.
    """
    key = str(court_id or "").strip().upper().replace("-", "_").replace(" ", "_")
    key = COURT_ALIASES.get(key, key)
    if key in COURT_RULES:
        return COURT_RULES[key]
    # F2 (2026-10-21): a forum derived into output/audit/forum_profiles.json renders from its DATA record
    # (pipeline.forum_profile) -- a new State is a profile, never a new COURT_RULES entry or caption branch.
    try:
        import sys as _sys
        _root = str(Path(__file__).resolve().parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from pipeline import forum_profile as _fp
        _r = _fp.render(court_id)
    except Exception:
        _r = None
    if _r:
        _r.setdefault("caption_template", "profile")
        return _r
    raise ValueError(
        "md_to_docx: unknown court id %r -- refusing to render a caption. "
        "Known: %s. Aliases: %s." % (
            court_id, ", ".join(sorted(COURT_RULES)), ", ".join(sorted(COURT_ALIASES)))
    )


# STANDALONE PAPERS (OJR rejection 2026-10-27, E-File #2610062538, verbatim: "ENSURE THE PROPOSED ORDER IS ON ITS OWN PAGE WITH THE
# CASE CAPTION AND CASE NUMBER"): a proposed order, verification and certificate of service are separate papers — each starts on
# its own page and carries the caption + case number again. Phila.Civ.R. *208.3(b)(3)(A)(ii) lists the proposed order as its own
# packet item; Pa.R.C.P. 208.2(a)(3). Per-court override via COURT_RULES[...]["standalone_sections"]; this is the default.
STANDALONE_SECTIONS_DEFAULT = ("[PROPOSED] ORDER", "PROPOSED ORDER", "ORDER", "CERTIFICATE OF SERVICE", "VERIFICATION")


def _standalone_heading(line: str, rules: dict):
    """'## [PROPOSED] ORDER' → 'PROPOSED ORDER' when the heading names a standalone paper for this court; else None."""
    m = re.match(r"^#{1,3}\s+(.+?)\s*$", line or "")
    if not m: return None
    head = re.sub(r"[\[\]]", "", m.group(1)).strip().upper()
    names = tuple(x.upper().replace("[", "").replace("]", "") for x in rules.get("standalone_sections", STANDALONE_SECTIONS_DEFAULT))
    return head if head in names else None


COURT_RULES = {
    "EDPA": {
        "jurisdiction": "EDPA",
        "court_full": "UNITED STATES DISTRICT COURT\nFOR THE EASTERN DISTRICT OF PENNSYLVANIA",
        "font": "Times New Roman", "size_pt": 12, "line_spacing": 1.5,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "federal",
        "is_federal": True, "is_appellate": False,
        "limits": {
            "motion":      {"pages": 25, "words": None},
            "brief":       {"pages": 25, "words": None},
            "reply":       {"pages": 15, "words": None},
        },
    },
    "3D_CIR": {
        "jurisdiction": "3DCIR",
        "court_full": "IN THE UNITED STATES COURT OF APPEALS\nFOR THE THIRD CIRCUIT",
        "font": "Times New Roman", "size_pt": 14, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "appellate_3rd_cir", "caption_compact": True,
        "is_federal": True, "is_appellate": True,
        # SPACER_AS_FORMAT is a per-court opt-in (FRAP 32(a)(4) double spacing is the only vertical rhythm; a blank md line
        # becomes a first-line indent, not an empty paragraph). Every other court keeps the legacy literal blank line —
        # PA_CP_PHILA / PA_SUPER renders must stay byte-identical (PROMOTION_READINESS_20261024: 77→40 paragraphs was RED).
        "spacer_mode": "format", "spacer_line_spacing": 1.0, "sig_block_line_spacing": 1.0,
        "limits": {
            # FRAP 32(a)(7); reduced 14000→13000 / 7000→6500 effective 12/1/2016
            "principal":   {"pages": 30, "words": 13000},
            "reply":       {"pages": 15, "words": 6500},
            "petition":    {"pages": None, "words": 7800},
            "amicus":      {"pages": None, "words": 6500},
            "cross_appellee_principal": {"pages": 35, "words": 15300},
            "cross_appellant_reply":    {"pages": None, "words": 13000},
            "cross_appellee_reply":     {"pages": None, "words": 6500},
        },
        "coc_required_if_over_pages": 30,
    },
    "SCOTUS": {
        "jurisdiction": "SCOTUS",
        "court_full": "IN THE SUPREME COURT OF THE UNITED STATES",
        "font": "Century Schoolbook", "size_pt": 12, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "scotus", "caption_compact": True,
        "is_federal": True, "is_appellate": True,
        # Pro se filers use Rule 33.2 (letter format) almost universally
        "format": "33.2_letter",  # vs "33.1_booklet"
        "limits": {
            # Rule 33.2(b) page limits (letter format, 8.5x11)
            "cert_petition":          {"pages": 40, "words": None},
            "jurisdictional_stmt":    {"pages": 40, "words": None},
            "extraordinary_writ":     {"pages": 40, "words": None},
            "opposition":             {"pages": 40, "words": None},
            "reply":                  {"pages": 15, "words": None},
            "supplemental":           {"pages": 15, "words": None},
            "rehearing":              {"pages": 15, "words": None},
        },
    },
    "PA_CP_PHILA": {
        "jurisdiction": "PA_CP",
        "court_full": "IN THE COURT OF COMMON PLEAS\nOF PHILADELPHIA COUNTY, PENNSYLVANIA\nCIVIL TRIAL DIVISION",
        "font": "Times New Roman", "size_pt": 12, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "state_pa_ccp",
        "is_federal": False, "is_appellate": False,
        "limits": {
            # No formal word limit at trial level; check local rules of CCP
            "motion":      {"pages": None, "words": None},
            "brief":       {"pages": None, "words": None},
        },
    },
    "PA_SUPER": {
        "jurisdiction": "PA_SUPER",
        "court_full": "IN THE SUPERIOR COURT OF PENNSYLVANIA",
        "font": "Times New Roman", "size_pt": 14, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "pa_superior", "caption_compact": True,
        "is_federal": False, "is_appellate": True,
        "limits": {
            # Pa.R.A.P. 2135(a)(1) general rule
            "principal":   {"pages": 30, "words": 14000},
            "reply":       {"pages": 15, "words": 7000},
            "cross_appeal_principal":  {"pages": 35, "words": 16500},
            "capital_direct_principal": {"pages": None, "words": 17500},
            "capital_direct_reply":     {"pages": None, "words": 8500},
        },
        "coc_required_if_over_pages": 30,
    },
    "PA_COMMWLTH": {
        "jurisdiction": "PA_COMMWLTH",
        "court_full": "IN THE COMMONWEALTH COURT OF PENNSYLVANIA",
        "font": "Times New Roman", "size_pt": 14, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "pa_commwlth", "caption_compact": True,
        "is_federal": False, "is_appellate": True,
        "limits": {
            "principal":   {"pages": 30, "words": 14000},
            "reply":       {"pages": 15, "words": 7000},
        },
        "coc_required_if_over_pages": 30,
    },
    "PA_SUPREME": {
        "jurisdiction": "PA_SUPREME",
        "court_full": "IN THE SUPREME COURT OF PENNSYLVANIA",
        "font": "Times New Roman", "size_pt": 14, "line_spacing": 2.0,
        "margin_in": 1.0, "page_numbers": True, "caption_template": "pa_supreme", "caption_compact": True,
        "is_federal": False, "is_appellate": True,
        "limits": {
            "principal":   {"pages": 30, "words": 14000},
            "reply":       {"pages": 15, "words": 7000},
            "capital_pcra_principal": {"pages": 49, "words": 22500},
            "capital_pcra_reply":     {"pages": 24, "words": 11250},
        },
        "coc_required_if_over_pages": 30,
    },
}


def _parse_block_md(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    text = _strip_html_comments(text)
    if text.startswith("\ufeff"):
        text = text[1:]
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except Exception:
                fm = {}
            body = parts[2].lstrip("\n")
            return fm, body
    return {}, text


_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]]+)\s*\}\}")


def _resolve_var(path: str, ctx: dict) -> str:
    """Resolve dotted/indexed var path. NEVER returns raw dict/list str dumps."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                idx = int(part)
                cur = cur[idx] if 0 <= idx < len(cur) else None
                continue
            except ValueError:
                return ""
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
        if cur is None:
            return ""
    if isinstance(cur, dict):
        # Special case: caption dict → readable short form
        if "plaintiff" in cur and "defendants" in cur:
            pl = cur.get("plaintiff", "")
            if isinstance(pl, dict):
                pl = pl.get("name", "")
            defs = cur.get("defendants") or []
            first_def = ""
            if isinstance(defs, list) and defs:
                d0 = defs[0]
                first_def = d0.get("name", "") if isinstance(d0, dict) else str(d0)
            parts = []
            if pl:
                # Last name only for short form
                last = str(pl).strip().split()[-1] if str(pl).strip() else ""
                parts.append(last or str(pl))
            if first_def:
                first_def_short = str(first_def).split(",")[0].split(" Inc")[0].split(" LLC")[0].strip()
                parts.append(f"v. {first_def_short} et al.")
            return " ".join(parts)
        # Pull a sensible scalar from the dict
        for k in ("name", "full_name", "title", "docket_no", "id"):
            if k in cur and cur[k]:
                return str(cur[k])
        return ""
    if isinstance(cur, list):
        names = []
        for item in cur:
            if isinstance(item, dict) and item.get("name"):
                names.append(item["name"])
            elif isinstance(item, str):
                names.append(item)
        return ", ".join(names) if names else ""
    return str(cur) if cur is not None else ""


def _substitute(text: str, ctx: dict) -> str:
    return _VAR_RE.sub(lambda m: _resolve_var(m.group(1), ctx), text)


_CMP_RE = re.compile(
    r"^\s*([a-zA-Z0-9_.\[\]]+)\s*(==|!=|>=|<=|>|<)\s*(true|false|\d+|\".*?\"|'.*?')\s*$"
)


def _eval_when(cond: str, ctx: dict) -> bool:
    if not cond or cond.strip() == "":
        return True
    m = _CMP_RE.match(cond)
    if not m:
        return True
    lhs_path, op, rhs = m.group(1), m.group(2), m.group(3)
    lhs_str = _resolve_var(lhs_path, ctx)
    if rhs == "true":
        rhs_val: Any = True
    elif rhs == "false":
        rhs_val = False
    elif rhs.startswith('"') or rhs.startswith("'"):
        rhs_val = rhs[1:-1]
    else:
        try:
            rhs_val = int(rhs)
        except ValueError:
            rhs_val = rhs
    try:
        if isinstance(rhs_val, bool):
            lhs_val: Any = lhs_str.lower() in ("true", "1", "yes")
        elif isinstance(rhs_val, int):
            lhs_val = int(lhs_str) if lhs_str else 0
        else:
            lhs_val = lhs_str
    except (ValueError, TypeError):
        return False
    if op == "==": return lhs_val == rhs_val
    if op == "!=": return lhs_val != rhs_val
    if op == ">":  return lhs_val > rhs_val
    if op == "<":  return lhs_val < rhs_val
    if op == ">=": return lhs_val >= rhs_val
    if op == "<=": return lhs_val <= rhs_val
    return False


def _build_caption_table(doc, ctx: dict, rules: dict):
    """Build the federal/state caption as a two-column table (traditional pleading layout).

    LEFT column: plaintiff name + address, 'Plaintiff,', 'v.', defendants, 'Defendants.'
    RIGHT column: 'Case No. XXXX', 'Judge XXXX', appellate info if applicable
    Separated by ')' divider lines, standard federal practice.
    """
    base_size = rules["size_pt"]
    # NJ CAPTION (S-nj-caption 2026-10-21) [corpus: nj_court_rule_1_4_1]: "Above the caption at the left-hand margin of
    # the first sheet of every paper to be filed there shall be printed or typed ... the name of such party, residence
    # address and telephone number." The court / division / county / docket sit in the caption's right column.
    _hdr = bool(rules.get("party_header_above_caption"))   # forum profile render flag
    if _hdr:
        _cap = ctx.get("caption") or {}
        _pn = _cap.get("plaintiff") or ctx.get("plaintiff") or ""
        _pn = _pn.get("name", "") if isinstance(_pn, dict) else _pn
        _pa = _cap.get("plaintiff_address") or ctx.get("plaintiff_address") or ""
        _ph = ctx.get("plaintiff_phone") or (_cap.get("plaintiff_phone") if isinstance(_cap, dict) else "") or ""
        for _hl in [str(_pn), *[x.strip() for x in str(_pa).split(",", 1) if x.strip()],
                    f"Telephone: {_ph or '____________________'}", "Plaintiff"]:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            r_ = p.add_run(_hl); r_.font.size = Pt(base_size)
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
        _md = ctx.get("_md_text") or ""
        _cm = re.search(r"Venue is (?:laid|proper) in ([A-Z][a-z]+) County", _md)
        _venue_county = (ctx.get("venue_county") or (_cm.group(1) if _cm else "") or "________").upper()
    # Court header above the table (NJ carries it in the caption's right column instead)
    for line in ([] if _hdr else rules["court_full"].split("\n")):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(line)
        run.bold = True
        run.font.size = Pt(base_size)
        p.paragraph_format.line_spacing = 1.0
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
    # Small spacer before the caption table
    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(6)
    sp.paragraph_format.line_spacing = 1.0

    # Build the left/right text content
    caption = ctx.get("caption") or {}
    plaintiff = caption.get("plaintiff") or ctx.get("plaintiff") or "PLAINTIFF NAME"
    if isinstance(plaintiff, dict):
        plaintiff = plaintiff.get("name", "PLAINTIFF NAME")
    plaintiff_addr = caption.get("plaintiff_address") or ctx.get("plaintiff_address") or ""

    defendants = caption.get("defendants") or ctx.get("defendants") or []
    def_lines = []
    if isinstance(defendants, list) and defendants:
        # CAPTION_ALL_DEFENDANTS_V1: the case caption is the structural identifier
        # of the action; every defendant of record appears in it regardless of
        # dismissal / default / judgment-by-default status. The prior status
        # filter ('active'|'default_available' only) dropped 3 of 6 defendants
        # in fed_26-cv-00123 (Avant/Equifax/Example Card Issuer) and ALL 3 defendants
        # in phila_ccp_260100000 (Lippi/Cacia/MGGT) from the caption.
        kept = [d for d in defendants
                if (isinstance(d, dict) and (d.get("name") or "").strip())
                or (isinstance(d, str) and str(d).strip())]
        for i, d in enumerate(kept):
            name = d.get("name", "") if isinstance(d, dict) else str(d)
            if not name:
                continue
            sep = ";" if i < len(kept) - 1 else ","
            def_lines.append(f"{name.upper()}{sep}")
    else:
        def_lines.append("DEFENDANT NAMES,")
    if rules.get("strip_caption_parentheticals"):   # descriptive parentheticals belong in the PARTIES averments, not the caption
        def_lines = [re.sub(r"\s*\([^)]*\)", "", d) for d in def_lines]

    court_info = ctx.get("court", {}) or {}
    docket = (court_info.get("docket_no") if isinstance(court_info, dict)
              else None) or ctx.get("docket_no") or "________"
    judge_obj = ctx.get("judge", {}) or {}
    judge_name = judge_obj.get("name", "") if isinstance(judge_obj, dict) else str(judge_obj)

    # Build LEFT column lines
    left_lines = []
    left_lines.append((f"{str(plaintiff).upper()},", True))
    if plaintiff_addr:
        left_lines.append((str(plaintiff_addr), False))
    left_lines.append(("", False))
    # PARTY DESIGNATIONS FOLLOW THE FORUM. In an appellate court the parties are the
    # Appellant and the Appellees; "Plaintiff/Defendants" are trial-court designations and
    # were emitted unconditionally here, so every Superior Court and Third Circuit paper
    # carried a trial caption. The 8/28 brief returned by the Superior Court Prothonotary
    # shows exactly this ("Plaintiff," / "Defendants." under "IN THE SUPERIOR COURT OF
    # PENNSYLVANIA"). A caption that misnames the parties' appellate roles is the first
    # thing the clerk reads and the easiest thing to bounce.
    _appellant = "Appellant," if rules.get("is_appellate") else "Plaintiff,"
    _appellees = "Appellees." if rules.get("is_appellate") else "Defendants."
    if rules.get("is_appellate") and len(def_lines) == 1:
        _appellees = "Appellee."
    left_lines.append((_appellant, False))
    left_lines.append(("", False))
    left_lines.append(("v.", False))
    left_lines.append(("", False))
    for dl in def_lines:
        left_lines.append((dl, False))
    left_lines.append(("", False))
    left_lines.append((_appellees, False))

    # Build RIGHT column lines (aligned roughly to plaintiff block)
    right_lines = []
    if rules.get("is_appellate"):
        appellate_docket = (ctx.get("appellate_docket")
                            or (court_info.get("appellate_docket") if isinstance(court_info, dict) else "")
                            or "________")
        right_lines.append((f"No. {appellate_docket}", True))
        if rules["jurisdiction"] == "3DCIR":
            right_lines.append(("", False))
            right_lines.append(("On Appeal from the United States", False))
            right_lines.append(("District Court for the Eastern", False))
            right_lines.append((f"District of Pennsylvania", False))
            right_lines.append((f"(D.C. No. {docket})", False))
    elif _hdr:
        right_lines += [(str(t).format(county=_venue_county, docket=docket), bool(bb))
                        for t, bb in rules.get("caption_right") or []]
    else:
        right_lines.append((f"Case No. {docket}", True))
        if ctx.get("control_no"):   # Phila.Civ.R. *208.3: the Motion Clerk's number "must be used on all Responses"
            right_lines.append((f"Control No. {ctx['control_no']}", True))
        if judge_name and rules.get("is_federal"):
            right_lines.append(("", False))
            right_lines.append((f"Judge {judge_name}", False))

    # Pad shorter side
    while len(right_lines) < len(left_lines):
        right_lines.append(("", False))
    while len(left_lines) < len(right_lines):
        left_lines.append(("", False))

    # CAPTION_COMPACT (user-locked 2026-10-25, Am. Jur. Pl. & Pr. Forms / 3d Cir. form caption): the caption box is not
    # "text" under FRAP 32(a)(4) / Pa.R.A.P. 124 — it is one compact block. Appellate profiles drop the padding rows
    # between party blocks (a 3d Cir. 5-page screening paper lost ~1/3 page to blank caption rows + wrapped columns).
    if rules.get("caption_compact"):
        def _squeeze(lines):
            out = []
            for t, b in lines:
                if not t and out and not out[-1][0]:
                    continue
                out.append((t, b))
            while out and not out[-1][0]:
                out.pop()
            return out
        left_lines, right_lines = _squeeze(left_lines), _squeeze(right_lines)
        while len(right_lines) < len(left_lines): right_lines.append(("", False))
        while len(left_lines) < len(right_lines): left_lines.append(("", False))

    # Build the 3-column table: LEFT | ) | RIGHT
    from docx.oxml.ns import nsmap, qn
    from docx.oxml import OxmlElement
    table = doc.add_table(rows=len(left_lines), cols=3)
    table.autofit = False
    # Width allocation: 3.0in / 0.3in / 3.2in (total 6.5in = usable width on 8.5in page w/ 1in margins)
    col_widths = [Inches(3.0), Inches(0.3), Inches(3.2)]
    for col_idx, w in enumerate(col_widths):
        for row in table.rows:
            row.cells[col_idx].width = w
    # CAPTION_GRID_WIDTHS (all courts): python-docx cell.width alone is ignored by LibreOffice/Word when the table has no
    # fixed layout + tblGrid, so every caption rendered with three EQUAL ~2.1in columns — "On Appeal from the United
    # States" and the party names wrapped and the box doubled in height (every CA3/Pa. Super. paper). Pin the grid.
    _tbl = table._tbl
    _tblPr = _tbl.tblPr
    _lay = OxmlElement("w:tblLayout"); _lay.set(qn("w:type"), "fixed"); _tblPr.append(_lay)
    _tw = _tblPr.find(qn("w:tblW"))
    if _tw is None:
        _tw = OxmlElement("w:tblW"); _tblPr.append(_tw)
    _tw.set(qn("w:w"), str(int(sum(col_widths) / 635))); _tw.set(qn("w:type"), "dxa")   # EMU -> twips
    _grid = _tbl.tblGrid
    for _gc, _w in zip(_grid.findall(qn("w:gridCol")), col_widths):
        _gc.set(qn("w:w"), str(int(_w / 635)))

    for i, ((l_text, l_bold), (r_text, r_bold)) in enumerate(zip(left_lines, right_lines)):
        # Left cell
        cell_l = table.rows[i].cells[0]
        cell_l.text = ""
        p_l = cell_l.paragraphs[0]
        p_l.paragraph_format.line_spacing = 1.0
        p_l.paragraph_format.space_before = Pt(0)
        p_l.paragraph_format.space_after = Pt(0)
        if l_text:
            run = p_l.add_run(l_text)
            run.font.size = Pt(base_size)
            if l_bold:
                run.bold = True
        # Middle ')' divider — only on non-blank rows (keeps it clean)
        cell_m = table.rows[i].cells[1]
        cell_m.text = ""
        p_m = cell_m.paragraphs[0]
        p_m.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_m.paragraph_format.line_spacing = 1.0
        p_m.paragraph_format.space_before = Pt(0)
        p_m.paragraph_format.space_after = Pt(0)
        if l_text or r_text:
            run_m = p_m.add_run(")")
            run_m.font.size = Pt(base_size)
        # Right cell
        cell_r = table.rows[i].cells[2]
        cell_r.text = ""
        p_r = cell_r.paragraphs[0]
        p_r.paragraph_format.line_spacing = 1.0
        p_r.paragraph_format.space_before = Pt(0)
        p_r.paragraph_format.space_after = Pt(0)
        if r_text:
            run = p_r.add_run(r_text)
            run.font.size = Pt(base_size)
            if r_bold:
                run.bold = True

    # Small spacer after the caption table
    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(6)
    sp.paragraph_format.line_spacing = 1.0


def _add_page_numbers(section):
    footer = section.footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in list(p.runs):
        r.text = ""
    run = p.add_run("Page ")
    fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
    instr_page = OxmlElement("w:instrText"); instr_page.text = "PAGE"
    fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin); run._r.append(instr_page); run._r.append(fld_end)
    p.add_run(" of ")
    run3 = p.add_run()
    fld_begin2 = OxmlElement("w:fldChar"); fld_begin2.set(qn("w:fldCharType"), "begin")
    instr_total = OxmlElement("w:instrText"); instr_total.text = "NUMPAGES"
    fld_end2 = OxmlElement("w:fldChar"); fld_end2.set(qn("w:fldCharType"), "end")
    run3._r.append(fld_begin2); run3._r.append(instr_total); run3._r.append(fld_end2)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITAL_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_UNDER_RE = re.compile(r"(?<!_)__([^\W_][^_]*?[^\W_]|[^\W_])__(?!_)")   # G3: underline markup only when the
#   content starts AND ends alphanumeric and the '__' delimiters are NOT part of an underscore run, so a
#   signature blank ('this _ day of __, 20____') is never eaten as __underline__ markup


def _apply_inline(paragraph, text: str, base_size: int):
    # RENDER_MD_BACKTICK_V1: source-cites like `legal memorandum.pdf` carry
    # backticks from the chain-stub drafter; unwrap them so they read as clean
    # text in the filed document (the citation text stays, only `` are removed).
    if "`" in text:
        text = text.replace("`", "")
    pos = 0
    while pos < len(text):
        bold_m = _BOLD_RE.search(text, pos)
        ital_m = _ITAL_RE.search(text, pos)
        under_m = _UNDER_RE.search(text, pos)
        candidates = [(m.start(), m, kind) for m, kind in
                      [(bold_m, "bold"), (ital_m, "italic"), (under_m, "underline")] if m]
        if not candidates:
            run = paragraph.add_run(text[pos:])
            run.font.size = Pt(base_size)
            break
        candidates.sort(key=lambda x: x[0])
        start, m, kind = candidates[0]
        if start > pos:
            run = paragraph.add_run(text[pos:start])
            run.font.size = Pt(base_size)
        run = paragraph.add_run(m.group(1))
        run.font.size = Pt(base_size)
        if kind == "bold": run.bold = True
        elif kind == "italic": run.italic = True
        elif kind == "underline": run.underline = True
        pos = m.end()


def _sanitize_for_filing(md_text: str) -> str:
    """G2: (1) DROP work-product that must never appear in a signed filing — Obsidian [[wikilinks]] and the
    internal '*Grounding: …*' provenance note; (2) close a **bold** span that the drafter hard-wrapped across
    a line (the per-line inline pass leaves the literal '**' otherwise). The join fires ONLY when the prior
    line has an odd number of '**' (an open span) and the next line is prose (not a heading/list/table), so
    captions, numbered errors, and signature blocks are never merged."""
    def _strip_wikilinks(s: str) -> str:
        s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)   # [[target|alias]] -> alias (keep the readable text)
        s = re.sub(r"\[\[[^\]]*\]\]", "", s)                 # [[target]] -> remove the bare reference
        return re.sub(r"\s{2,}", " ", s).rstrip()
    out = []
    for raw in (md_text or "").split("\n"):
        ln = raw.rstrip()
        if re.match(r"^\s*[*_]*Grounding:", ln):
            continue   # internal provenance note — never in a signed filing
        if "[[" in ln and "PAGEBREAK" not in ln:
            # HARDEN, don't strip the sentence: remove only the [[wikilink]] token and keep the prose. Drop
            # the line entirely ONLY when nothing substantive remains (a bare [[link]] or a Related-links item).
            stripped = _strip_wikilinks(ln)
            core = re.sub(r"^[\s\-*>|#0-9.)]+", "", stripped)
            if len(core) < 15:
                continue
            ln = stripped
        if (out and out[-1].count("**") % 2 == 1 and ln.strip()
                and not re.match(r"^\s*(#{1,6}\s|\d+[.)]\s|[-*]\s|\||>|\[\[)", ln)):
            out[-1] = out[-1].rstrip() + " " + ln.strip()   # close the wrapped bold span on one logical line
        else:
            out.append(ln)
    return "\n".join(out)


_NUM_PARA_RE = re.compile(r"^(\d+)\.\s+(.+)$")


def _render_line(doc, line: str, rules: dict, allow_blank: bool = False):
    """Render one markdown line. allow_blank=True only for renderer-emitted spacers."""
    line = line.rstrip()
    # PAGEBREAK marker -> hard page break (cert of service / proposed order)
    if line.strip() == "[[PAGEBREAK]]":
        from docx.enum.text import WD_BREAK as _WB
        _pp = doc.add_paragraph(); _pp.add_run().add_break(_WB.PAGE)
        return
    base_size = rules["size_pt"]
    align = None
    if "::center" in line:
        line = line.replace("::center", "").rstrip()
        align = WD_ALIGN_PARAGRAPH.CENTER
    elif "::right" in line:
        line = line.replace("::right", "").rstrip()
        align = WD_ALIGN_PARAGRAPH.RIGHT
    elif "::justify" in line:
        line = line.replace("::justify", "").rstrip()
        align = WD_ALIGN_PARAGRAPH.JUSTIFY
    if not line.strip():
        # Default: skip blank lines from block content (line spacing handles gaps)
        if not allow_blank:
            return
        # SPACER_AS_FORMAT (CA3 26-0000 v17: 34 empty paragraphs in a 5-page paper). A blank md line is a paragraph
        # boundary, not content: no empty paragraph is emitted; the NEXT body paragraph carries the break in its own
        # paragraph format (first-line indent for prose; nothing for short lines such as the signature block), so
        # FRAP 32(a)(4) double spacing is the only vertical rhythm. rules["spacer_mode"]="paragraph" restores the old
        # behaviour for a court whose profile wants a literal blank line.
        if rules.get("spacer_mode", "paragraph") == "format":   # default = legacy literal blank line (all other courts unchanged)
            doc._m1_gap = True
            return
        # Renderer-emitted spacers ARE honored
        last = doc.paragraphs[-1] if doc.paragraphs else None
        if last is not None and not last.text.strip():
            return
        p = doc.add_paragraph()
        # a spacer is an empty gap, not text: single-spaced (the court's double spacing governs text lines). A
        # double-spaced spacer after every block cost ~1 page per 5-page appellate paper (CA3 26-0000 v15/v16).
        p.paragraph_format.line_spacing = rules.get("spacer_line_spacing", rules["line_spacing"])   # legacy: court spacing
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        return
    _gap = bool(getattr(doc, "_m1_gap", False))
    doc._m1_gap = False
    if line.startswith("### "):
        text = line[4:].strip()
        p = doc.add_paragraph()
        p.alignment = align or WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run(text); run.bold = True; run.font.size = Pt(base_size)
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.0   # DOCX_COMPACT_V1
        return
    if line.startswith("## "):
        doc._m1_sig = False
        text = line[3:].strip()
        p = doc.add_paragraph()
        p.alignment = align or WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text.upper()); run.bold = True; run.font.size = Pt(base_size)
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.0   # DOCX_COMPACT_V1
        return
    if line.startswith("# "):
        text = line[2:].strip()
        p = doc.add_paragraph()
        p.alignment = align or WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text.upper()); run.bold = True; run.font.size = Pt(base_size + 2)
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(8)
        p.paragraph_format.line_spacing = 1.0   # DOCX_COMPACT_V1
        return
    if line.strip() in ("---", "***", "___"):
        p = doc.add_paragraph()
        run = p.add_run(); run.add_break()
        return
    m_num = _NUM_PARA_RE.match(line)
    if m_num:
        num, body = m_num.group(1), m_num.group(2)
        p = doc.add_paragraph()
        p.alignment = align or WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.left_indent = Inches(0.5)        # DOCX_COMPACT_V1
        p.paragraph_format.first_line_indent = Inches(-0.5) # hanging indent
        p.paragraph_format.line_spacing = rules["line_spacing"]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(f"{num}.\t"); run.font.size = Pt(base_size)
        run.font.name = rules["font"]
        _apply_inline(p, body, base_size)
        return
    # Lettered sub-item: 'a. text'/'A. text' (upper or lower), or '  a. text'
    stripped = line.lstrip()
    if len(stripped) >= 3 and stripped[0].isalpha() and stripped[1:3] == ". ":
        letter = stripped[0]
        body = stripped[3:]
        p = doc.add_paragraph()
        p.alignment = align or WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.left_indent = Inches(0.75)
        p.paragraph_format.first_line_indent = Inches(-0.25)
        p.paragraph_format.line_spacing = rules["line_spacing"]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(f"{letter}.\t"); run.font.size = Pt(base_size)
        run.font.name = rules["font"]
        _apply_inline(p, body, base_size)
        return
    if line.lstrip().startswith(("- ", "* ")):
        text = line.lstrip()[2:]
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.line_spacing = 1.0   # DOCX_COMPACT_V1
        p.paragraph_format.space_after = Pt(0)
        _apply_inline(p, text, base_size)
        return
    p = doc.add_paragraph()
    p.alignment = align or WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.line_spacing = rules["line_spacing"]
    # SIG_BLOCK_SINGLE (per-court opt-in rules["sig_block_line_spacing"]): from "Respectfully submitted," to the next
    # heading, the signature block is not text under FRAP 32(a)(4) and is conventionally single-spaced. A double-spaced
    # 9-line block cost ~1/3 page per 5-page appellate paper (CA3 26-0002 v36/v37 landed on page 6 by the /s/ lines alone).
    _sig = rules.get("sig_block_line_spacing")
    if _sig:
        if re.match(r"^\s*Respectfully submitted", line):
            doc._m1_sig = True
        if getattr(doc, "_m1_sig", False):
            p.paragraph_format.line_spacing = _sig
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    # SPACER_AS_FORMAT: a prose paragraph that followed a blank line opens with a first-line indent (short lines —
    # signature block, "Dated:", "Respectfully submitted," — are not prose and take none).
    if _gap and align is None and len(line.strip()) >= rules.get("prose_min_chars", 100):
        p.paragraph_format.first_line_indent = Inches(rules.get("first_line_indent_in", 0.5))
    _apply_inline(p, line, base_size)


def _setup_document(rules: dict) -> Document:
    doc = Document()
    for section in doc.sections:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(rules["margin_in"])
        section.bottom_margin = Inches(rules["margin_in"])
        section.left_margin = Inches(rules["margin_in"])
        section.right_margin = Inches(rules["margin_in"])
        if rules.get("page_numbers", True):
            _add_page_numbers(section)
    style = doc.styles["Normal"]
    style.font.name = rules["font"]
    style.font.size = Pt(rules["size_pt"])
    pf = style.paragraph_format
    pf.line_spacing = rules["line_spacing"]
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)
    return doc


_BREAK_PATTERNS = (
    "# ", "## ", "### ", "#### ",
    "- ", "* ",
    "---", "***", "___",
)


def _is_paragraph_break(line: str) -> bool:
    """Return True if this line should start its own paragraph (not be coalesced)."""
    stripped = line.lstrip()
    if not stripped:
        return True
    for prefix in _BREAK_PATTERNS:
        if stripped.startswith(prefix):
            return True
    # Alignment markers force paragraph break
    if "::right" in line or "::center" in line or "::justify" in line:
        return True
    # Numbered paragraph "1. " "2. " ...
    if _NUM_PARA_RE.match(stripped):
        return True
    # Lettered sub-item "a. " "b. " up to "z. "
    if len(stripped) >= 3 and stripped[0].isalpha() and stripped[0].islower() and stripped[1:3] == ". ":
        return True
    return False


def _render_block_body(doc, body: str, ctx: dict, rules: dict):
    """Render a block body. Coalesces consecutive non-break lines into single paragraphs.

    Markdown convention: single newlines join lines into one paragraph; blank
    lines separate paragraphs. Headers, lists, blockquotes, and numbered items
    always start a new paragraph; consecutive `>` lines coalesce as one quote.
    """
    lines = body.split("\n")
    buf: list[str] = []
    quote_buf: list[str] = []

    def flush_prose():
        nonlocal buf
        if not buf:
            return
        joined = " ".join(s.strip() for s in buf if s.strip()).strip()
        buf = []
        if joined:
            _render_line(doc, _substitute(joined, ctx), rules, allow_blank=False)

    def flush_quote():
        nonlocal quote_buf
        if not quote_buf:
            return
        # Strip the leading '>' and coalesce
        stripped_lines = []
        for ln in quote_buf:
            s = ln.lstrip()
            if s.startswith(">"):
                s = s[1:].lstrip()
            stripped_lines.append(s.strip())
        joined = " ".join(s for s in stripped_lines if s).strip()
        quote_buf = []
        if joined:
            base_size = rules["size_pt"]
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.line_spacing = rules["line_spacing"]
            p.paragraph_format.left_indent = Inches(0.5)
            p.paragraph_format.right_indent = Inches(0.5)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            _apply_inline(p, _substitute(joined, ctx), base_size)

    for raw in lines:
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith(">"):
            flush_prose()
            quote_buf.append(line)
            continue
        # Non-quote line: flush any pending quote
        flush_quote()
        if _is_paragraph_break(line):
            flush_prose()
            _render_line(doc, _substitute(line, ctx), rules, allow_blank=False)
        else:
            buf.append(line)
    flush_prose()
    flush_quote()
    _render_line(doc, "", rules, allow_blank=True)


# BEEZLE_FN_UNUSED 2026-06-13: build_filing_from_blocks (no static refs in pipeline/)
def build_filing_from_blocks(
    filing_type: str,
    case_ground_truth: dict,
    out_path: str,
    court_id: str = "EDPA",
    extra_context: Optional[dict] = None,
    body_md: Optional[str] = None,
) -> dict:
    rules = _court_rules_or_die(court_id)
    ctx = dict(case_ground_truth)
    ctx["is_federal"] = rules["is_federal"]
    ctx["is_appellate"] = rules["is_appellate"]
    ctx["court_id"] = court_id
    if extra_context:
        ctx.update(extra_context)
    ctx.setdefault("defect_categories", {})
    ctx.setdefault("cross_case_count", 0)
    # Content-variable defaults (empty string → unresolved {{vars}} disappear)
    _content_defaults = {
        "defect_total": "", "fatal_count": "", "posture": "active",
        "factual_app_default": "",
        "factual_app_fcra_willful": "",
        "factual_app_fcra_negligent": "",
        "factual_app_fdcpa_false": "",
        "factual_app_fdcpa_unfair": "",
        "factual_app_utpcpl": "",
    }
    for k, v in _content_defaults.items():
        ctx.setdefault(k, v)
    if "registry" not in ctx:
        court_obj = ctx.get("court", {}) or {}
        docket = court_obj.get("docket_no", "") if isinstance(court_obj, dict) else ""
        ctx["registry"] = {
            "docket_aliases": [docket],
            "appellate_docket": ctx.get("appellate_docket", ""),
        }
    spec_path = FILING_TYPES_DIR / f"{filing_type}.yaml"
    if not spec_path.exists():
        return {"ok": False, "error": f"filing-type spec not found: {spec_path}",
                "out_path": out_path}
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    structure = spec.get("structure", [])

    # Pre-render exhibit list if exhibits exist
    exhibit_list_md = ""
    exhibits = ctx.get("exhibits") or []
    if exhibits:
        try:
            from generation.exhibit_handler import classify_exhibits, render_exhibit_list_block
            classified = classify_exhibits(ctx)
            exhibit_list_md = render_exhibit_list_block(classified)
        except Exception:
            pass

    doc = _setup_document(rules)
    used, skipped = [], []
    title = spec.get("title", filing_type.replace("_", " ").title())
    caption_emitted = False
    body_inserted = False
    exhibit_list_emitted = False

    for item in structure:
        block_path = item.get("block")
        when = item.get("when", "")
        if not _eval_when(when, ctx):
            skipped.append({"block": block_path, "reason": f"when='{when}' false"})
            continue

        # Caption (renderer-built as 2-column table)
        if block_path and block_path.startswith("boilerplate/caption_"):
            if caption_emitted:
                skipped.append({"block": block_path, "reason": "caption already emitted"})
                continue
            _build_caption_table(doc, ctx, rules)
            # Filing title centered, bold
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(title.upper())
            run.bold = True
            run.font.size = Pt(rules["size_pt"] + 2)
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(12)
            caption_emitted = True
            used.append({"block": block_path, "source": "renderer-built caption table"})
            continue

        # Page break before Certificate of Service and Proposed Order
        if block_path in ("boilerplate/certificate_of_service", "boilerplate/proposed_order"):
            p_br = doc.add_paragraph()
            p_br.add_run().add_break(WD_BREAK.PAGE)

        # Inject exhibit list before signature block
        if (block_path == "boilerplate/signature_block_pro_se"
                and exhibit_list_md and not exhibit_list_emitted):
            _render_block_body(doc, exhibit_list_md, ctx, rules)
            exhibit_list_emitted = True
            used.append({"block": "<auto exhibit_list>", "source": "exhibit_handler"})

        full = BLOCKS_DIR / f"{block_path}.md"
        if not full.exists():
            skipped.append({"block": block_path, "reason": "file not found"})
            continue
        fm, body = _parse_block_md(full)

        # Render this block
        _render_block_body(doc, body, ctx, rules)
        used.append({"block": block_path, "source": str(full)})

        # After standard_of_review, inject custom body_md (proper legal structure)
        if not body_inserted and block_path == "sections/standard_of_review" and body_md:
            _render_block_body(doc, body_md, ctx, rules)
            body_inserted = True
            used.append({"block": "<custom body_md>", "source": "extra_context.body_md"})

    # Run word count check; for appellate courts, append a Certificate of Compliance
    brief_type = (extra_context or {}).get("brief_type")
    word_check = None
    if rules.get("is_appellate") and brief_type:
        word_count = count_document_words(doc)
        word_check = check_word_limit(doc, rules, brief_type)
        if word_check.get("requires_coc"):
            render_certificate_of_compliance(doc, rules, brief_type, word_count)

    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_p))
    result = {
        "ok": True, "out_path": str(out_p),
        "blocks_used": used, "blocks_skipped": skipped,
        "filing_type": filing_type, "court_id": court_id,
    }
    if word_check is not None:
        result["word_check"] = word_check
    return result


def count_document_words(doc) -> int:
    """Count words in a docx Document body, excluding tables (caption table) and footers.

    FRAP 32(f) and Pa.R.A.P. 2135 exclude: cover, TOC, TOA, certificate of service,
    certificate of compliance, signature block, addendum. For now we count the
    full body (less footer/header) and rely on the brief_type limit being inclusive.
    """
    n = 0
    for p in doc.paragraphs:
        if p.text.strip():
            n += len(p.text.split())
    return n


def check_word_limit(doc, rules: dict, brief_type: str) -> dict:
    """Check whether the produced doc exceeds the limit for the given brief_type.

    Returns dict with: word_count, page_limit, word_limit, over_pages, over_words,
    requires_coc (bool).
    """
    limits = rules.get("limits", {}) or {}
    spec = limits.get(brief_type) or {}
    word_count = count_document_words(doc)
    page_count = None  # python-docx doesn't compute pagination
    word_limit = spec.get("words")
    page_limit = spec.get("pages")
    coc_trigger = rules.get("coc_required_if_over_pages")
    return {
        "brief_type": brief_type,
        "word_count": word_count,
        "word_limit": word_limit,
        "page_limit": page_limit,
        "over_words": (word_limit is not None and word_count > word_limit),
        "requires_coc": (word_limit is not None),
        "coc_trigger_pages": coc_trigger,
    }


def render_certificate_of_compliance(doc, rules: dict, brief_type: str,
                                      word_count: int) -> None:
    """Append a Certificate of Compliance per FRAP 32(g) or Pa.R.A.P. 2135.

    Distinct from Certificate of Service. Required when filing under word-count
    rule. Federal form 6 / PA equivalent.
    """
    base_size = rules["size_pt"]
    limits = rules.get("limits", {}) or {}
    spec = limits.get(brief_type) or {}
    word_limit = spec.get("words")
    if word_limit is None:
        return  # No CoC needed
    # Page break before CoC
    p_br = doc.add_paragraph()
    p_br.add_run().add_break(WD_BREAK.PAGE)
    # Header
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("CERTIFICATE OF COMPLIANCE")
    run.bold = True
    run.font.size = Pt(base_size)
    p.paragraph_format.space_after = Pt(12)
    p.paragraph_format.line_spacing = rules["line_spacing"]
    # Body
    juris = rules["jurisdiction"]
    if juris in ("3DCIR",):
        body_text = (
            f"This brief complies with the type-volume limitation of Federal "
            f"Rule of Appellate Procedure 32(a)(7)(B) because, excluding the "
            f"parts of the brief exempted by Rule 32(f), this brief contains "
            f"{word_count:,} words. This brief complies with the typeface "
            f"requirements of Rule 32(a)(5) and the type-style requirements "
            f"of Rule 32(a)(6) because this brief has been prepared in a "
            f"proportionally spaced typeface using Microsoft Word in "
            f"{rules['font']} {rules['size_pt']}-point font."
        )
    elif juris in ("PA_SUPER", "PA_COMMWLTH", "PA_SUPREME"):
        body_text = (
            f"I certify that this brief complies with the word count "
            f"limitation of Pa.R.A.P. 2135. Per the word count of the "
            f"word processing system used to prepare this brief, this "
            f"brief contains {word_count:,} words, which does not exceed "
            f"the {word_limit:,}-word limit applicable to a "
            f"{brief_type.replace('_', ' ')}."
        )
    elif juris == "SCOTUS":
        body_text = (
            f"I certify that this {brief_type.replace('_', ' ')} complies "
            f"with the page limit of Supreme Court Rule 33.2(b) "
            f"applicable to unrepresented filers appearing personally on 8.5 x 11 inch paper. This "
            f"document contains {word_count:,} words and does not exceed "
            f"the page limit of {spec.get('pages')} pages."
        )
    else:
        body_text = (
            f"I certify that this filing complies with the applicable "
            f"length limitations. Per the word count of the word "
            f"processing system used, this document contains "
            f"{word_count:,} words."
        )
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.line_spacing = rules["line_spacing"]
    p.paragraph_format.space_after = Pt(12)
    run = p.add_run(body_text)
    run.font.size = Pt(base_size)
    # Signature
    p = doc.add_paragraph()
    p.add_run("_____________________________________").font.size = Pt(base_size)
    p = doc.add_paragraph()
    p.add_run("FILER NAME, sui juris").font.size = Pt(base_size)
    p = doc.add_paragraph()
    p.add_run("Dated: ___________________________").font.size = Pt(base_size)


def _strip_raw_caption_from_body(md_text: str, rules: dict) -> tuple[str, str]:
    """RENDER_MD_CAPTION_V1: remove the raw caption scaffolding the chain-stub
    drafter emits at the top of the body so the renderer-built caption is the
    only one. Returns (title, cleaned_body). title is taken from the leading
    '# <TITLE>' line if present (the caption builder re-emits it centered)."""
    header_lines = set()
    for hl in (rules.get("court_full") or "").split("\n"):
        if hl.strip():
            header_lines.add(hl.strip().upper())
    title = ""
    lines = md_text.split("\n")
    out = []
    # We strip leftover CAPTION pipe-rows only while still in the pre-body
    # preamble (before any real section heading or body text). Once body
    # content starts, ALL pipe-rows are data tables (Defects / IF-THEN) and
    # MUST be preserved.
    in_preamble = True
    _title_taken_idx = -1
    for idx, raw in enumerate(lines):
        s = raw.strip()
        u = s.upper()
        # leading document title -> capture, drop (caption builder re-emits it)
        if not title and s.startswith("# ") and "APPENDIX" not in u:
            title = s[2:].strip()
            _title_taken_idx = idx
            # multi-line title: fold a bare ALL-CAPS continuation line
            # (e.g. "AND FOR SANCTIONS") that immediately follows
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if nxt and nxt.upper() == nxt and not nxt.startswith(("#", "|", ">", "-", "1", "2")) \
               and len(nxt.split()) <= 6:
                title = title + " " + nxt
            continue
        # skip a continuation line we just folded into the title
        if _title_taken_idx >= 0 and idx == _title_taken_idx + 1 and s and title.endswith(s):
            continue
        # mark end of preamble at first heading or substantive body line
        if in_preamble and s and not s.startswith("|") and u not in header_lines \
           and not u.startswith(("COURT:", "DOCKET:")):
            in_preamble = False
        # raw markdown caption table rows: drop ONLY in the preamble
        if in_preamble and s.startswith("|") and s.endswith("|"):
            continue
        # duplicated plain court-header lines
        if u in header_lines:
            continue
        # provenance header lines
        if u.startswith("COURT:") or u.startswith("DOCKET:"):
            continue
        out.append(raw)
    while out and not out[0].strip():
        out.pop(0)
    return title, "\n".join(out)



def _is_md_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2

def _is_md_table_sep(line: str) -> bool:
    s = line.strip()
    if not (s.startswith("|") and "-" in s):
        return False
    cells = [c.strip() for c in s.strip("|").split("|")]
    return all(set(c) <= set("-: ") and "-" in c for c in cells if c != "")

def _split_md_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]

def _render_md_table(doc, header, rows, rules):
    from docx.shared import Pt as _Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _WD
    ncols = len(header)
    t = doc.add_table(rows=1 + len(rows), cols=ncols)
    t.style = "Table Grid"
    t.autofit = True
    # header
    for j, h in enumerate(header):
        c = t.rows[0].cells[j]
        c.text = ""
        p = c.paragraphs[0]
        r = p.add_run(h)
        r.bold = True
        r.font.size = _Pt(9)
    # body
    for i, row in enumerate(rows, 1):
        for j in range(ncols):
            val = row[j] if j < len(row) else ""
            cell = t.rows[i].cells[j]
            cell.text = ""
            p = cell.paragraphs[0]
            rr = p.add_run(val)
            rr.font.size = _Pt(9)
    # small spacer after
    doc.add_paragraph().paragraph_format.space_after = _Pt(2)


def render_md_to_docx(md_text: str, out_path: str, court_id: str = "EDPA",
                       court_rules_path: Optional[str] = None,
                       ctx: Optional[dict] = None) -> str:
    """Legacy single-pass markdown→docx.

    When ctx is provided, build the court header + 2-column caption via the same
    _build_caption_table the block path uses (header on top, caption below), and
    strip the raw caption scaffolding from the body first. When ctx is None,
    behaves exactly as before (caller supplied its own caption in the md)."""
    md_text = _sanitize_for_filing(_strip_html_comments(md_text or ''))
    rules = _court_rules_or_die(court_id)
    doc = _setup_document(rules)
    if ctx:
        title, md_text = _strip_raw_caption_from_body(md_text, rules)
        ctx = {**ctx, "_md_text": md_text}
        _build_caption_table(doc, ctx, rules)
        if title:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(title.upper())
            run.bold = True
            run.font.size = Pt(rules["size_pt"] + 2)
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(12)
    _md_lines = md_text.split("\n")
    # QUANTUM FOOTNOTE SMALL-FONT: the "DEFINITIONS & AUTHORITIES —" block (emitted by
    # definitions_tagger.render_footnotes and appended at the very end of the draft) is footnote text.
    # Once we hit that sentinel, render the remainder at real footnote size (single-spaced, ~9pt).
    _fn_rules = dict(rules); _fn_rules["size_pt"] = 9; _fn_rules["line_spacing"] = 1.0
    _in_footnotes = False
    _i = 0
    while _i < len(_md_lines):
        _ln = _md_lines[_i]
        if not _in_footnotes and _ln.strip().startswith("DEFINITIONS & AUTHORITIES"):
            _in_footnotes = True
        _cur = _fn_rules if _in_footnotes else rules
        # markdown table block: header row + separator row
        if _is_md_table_row(_ln) and (_i + 1) < len(_md_lines) and _is_md_table_sep(_md_lines[_i + 1]):
            _header = _split_md_row(_ln)
            _rows = []
            _j = _i + 2
            while _j < len(_md_lines) and _is_md_table_row(_md_lines[_j]):
                _rows.append(_split_md_row(_md_lines[_j]))
                _j += 1
            _render_md_table(doc, _header, _rows, _cur)
            _i = _j
            continue
        if ctx and _standalone_heading(_ln, rules) and _i > 0:
            # new paper: own page, caption + case number again, then the heading
            _pb = doc.add_paragraph(); _pb.add_run().add_break(WD_BREAK.PAGE)
            _build_caption_table(doc, ctx, rules)
        _render_line(doc, _ln, _cur, allow_blank=True)
        _i += 1
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_p))
    return str(out_p)
