"""
governing_text_gate — deterministic gate + placeholder hydration for the drafting block
"Governing Text — Legal Evidence of the Law"
(blocks: sections/governing_text_legal_evidence_federal, sections/governing_text_legal_evidence_pa).

WHY: courts and opposing counsel re-litigate what the statute IS from the codified text plus case
gloss. The block states up front which text is the legal evidence of the law and asks for nothing
discretionary. It is emitted ONLY when the distinction can matter; otherwise it is omitted — no
boilerplate.

GATE (all deterministic, no LLM):
  (a) a count rests on a U.S.C. title that is NOT enacted into positive law
      (1 U.S.C. § 204(a) + the "United States Code Titles as Positive Law" note —
       [corpus:USCODE-LII-title1#aed75486c1] [corpus:USCODE-LII-title1#f4f0467520]
       [corpus:USCODE-LII-title1#1c82dfcead]);
  (b) a Code/enactment or regulation/statute divergence is flagged on the case
      (CASE_GROUND_TRUTH.yaml `governing_text.divergence_flagged: true`, or any cause/claim carrying
       both `enacted_by` and `codified_at`, or a check_conformity report with conforms == false
       stored at `conformity_results`);
  (c) forum = Pennsylvania and a count rests on a P.S. (Purdon's) citation rather than Pa.C.S.

Everything the block prints is pulled from the context dict this module hydrates. Chunk ids below
resolve in ~/turbovec/chunk_text.db (chunk_id = f"{source}#{sha1(text[:650])[:10]}").
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
from typing import Any, Iterable

# ── 1 U.S.C. § 204 note, "United States Code Titles as Positive Law" (verbatim list, corpus) ─────
# [corpus:USCODE-LII-title1#aed75486c1] Titles 1,3,4,5,9,10,11,13,14,17,18,23,28,31,32,(34→see 10),35
# [corpus:USCODE-LII-title1#f4f0467520] Titles 36,37,38,39,40,41,44,46,49,51,54
# [corpus:USCODE-LII-title1#1c82dfcead] Title 26 "enacted in the form of a separate code" (IRC 1954/1986)
# Title 34 is listed only as "Navy—See Title 10"; the present Title 34 (Crime Control) is NOT listed
# as enacted, so it is deliberately absent from the set.
POSITIVE_LAW_TITLES: frozenset[int] = frozenset({
    1, 3, 4, 5, 9, 10, 11, 13, 14, 17, 18, 23, 26, 28, 31, 32, 35, 36, 37, 38, 39, 40, 41, 44, 46,
    49, 51, 54,
})
POSITIVE_LAW_NOTE_CHUNKS = (
    "USCODE-LII-title1#aed75486c1",
    "USCODE-LII-title1#f4f0467520",
    "USCODE-LII-title1#1c82dfcead",
)

# Enactment credits (verbatim source credits from the U.S.C. section notes in corpus) for the titles
# the active cases actually plead. Anything not here renders as "the Act as enacted (Statutes at
# Large)" with no fabricated Pub. L. number.
ENACTMENT_CREDITS: dict[str, dict[str, str]] = {
    # FCRA — 15 U.S.C. § 1681 et seq.
    "15:1681": {
        "act": "Fair Credit Reporting Act",
        "enacted_by": "Pub. L. 91–508, title VI, § 601, Oct. 26, 1970, 84 Stat. 1128 "
                      "(adding title VI to Pub. L. 90–321), as amended, incl. Pub. L. 104–208, "
                      "div. A, title II, Sept. 30, 1996, 110 Stat. 3009–426 et seq.",
        "corpus": "[corpus:usc_t15_s1681#1bdaa7478d] [corpus:usc_t15_s1681i#2650ee6828] "
                  "[corpus:usc_t15_s1681n#9ec6afc8d4] [corpus:usc_t15_s1681s–2#495ba0aa99]",
    },
    # FDCPA — 15 U.S.C. § 1692 et seq.
    "15:1692": {
        "act": "Fair Debt Collection Practices Act",
        "enacted_by": "Pub. L. 95–109, Sept. 20, 1977, 91 Stat. 874 (adding title VIII to "
                      "Pub. L. 90–321), as amended",
        "corpus": "[corpus:usc_t15_s1692#bb087a4887] [corpus:usc_t15_s1692e#46a6143d93]",
    },
}

_USC_RE = re.compile(r"\b(\d{1,2})\s*U\.?\s*S\.?\s*C\.?\s*(?:§+|[Ss]ec(?:tion)?s?\.?)?\s*(\d+[a-z]?(?:[-–]\d+)?)?", re.I)
_PACS_RE = re.compile(r"\b(\d{1,2})\s*Pa\.?\s*C\.?\s*S\.?(?:A\.?)?\s*§*", re.I)
_PS_RE = re.compile(r"\b(\d{1,2})\s*P\.\s*S\.\s*§+\s*([\d.\-–]+[a-z]?)", re.I)


def _sec_key(sec: str):
    """Natural sort for section numbers: 1681e < 1681i < 1681s-2 < 1692e."""
    m = re.match(r"(\d+)([a-z]?)(?:-(\d+))?", sec)
    if not m:
        return (10**9, sec, 0)
    return (int(m.group(1)), m.group(2) or "", int(m.group(3) or 0))


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────
def _iter_strings(obj: Any, depth: int = 0) -> Iterable[str]:
    """Yield every string in a nested dict/list (bounded depth)."""
    if depth > 6 or obj is None:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v, depth + 1)


def _authority_strings(ctx: dict) -> list[str]:
    """Only the fields that state what a COUNT rests on — never free prose (no false triggers)."""
    out: list[str] = []
    for c in ctx.get("causes_of_action") or []:
        if isinstance(c, dict):
            out.append(str(c.get("citation") or ""))
            out.append(str(c.get("enacted_by") or ""))
            out.append(str(c.get("codified_at") or ""))
    gt = ctx.get("ground_truth") or {}
    if not isinstance(gt, dict):
        gt = {}
    if not gt and any(k in ctx for k in ("defendants", "pending_motions", "factual_application", "governing_text")):
        gt = ctx  # md_to_docx.build_filing_from_blocks passes the CASE_GROUND_TRUTH dict itself as ctx
    for key in ("claims", "counts", "statutes", "causes_of_action"):
        v = gt.get(key)
        if v:
            out.extend(_iter_strings(v))
    for d in gt.get("defendants") or []:
        if isinstance(d, dict):
            out.extend(_iter_strings(d.get("claims")))
    fa = gt.get("factual_application") or ctx.get("factual_application") or {}
    if isinstance(fa, dict):
        out.extend(_iter_strings(fa))
    # NOTE: pending_motions[].primary_authority is deliberately NOT read — motion mechanics
    # (FRAP 4, 28 U.S.C. § 1291) are not what a COUNT rests on and must not trigger or pad the block.
    g = gt.get("governing_text") or ctx.get("governing_text") or {}
    if isinstance(g, dict):
        out.extend(_iter_strings(g))
    return [s for s in out if s]


def usc_titles_cited(strings: Iterable[str]) -> dict[int, set[str]]:
    """{title: {sections}} for every 'NN U.S.C. § xxxx' in the given strings."""
    found: dict[int, set[str]] = {}
    for s in strings:
        for m in _USC_RE.finditer(s):
            try:
                t = int(m.group(1))
            except ValueError:
                continue
            if 1 <= t <= 54:
                found.setdefault(t, set())
                if m.group(2):
                    found[t].add(m.group(2).replace("–", "-"))
    return found


def ps_cites(strings: Iterable[str]) -> list[str]:
    return sorted({f"{m.group(1)} P.S. § {m.group(2)}" for s in strings for m in _PS_RE.finditer(s)})


def pacs_titles(strings: Iterable[str]) -> set[int]:
    return {int(m.group(1)) for s in strings for m in _PACS_RE.finditer(s)}


def _divergence_flag(ctx: dict) -> tuple[bool, str]:
    gt = ctx.get("ground_truth") or {}
    g = (gt.get("governing_text") if isinstance(gt, dict) else None) or ctx.get("governing_text") or {}
    if isinstance(g, dict) and str(g.get("divergence_flagged", "")).lower() in ("true", "1", "yes"):
        return True, str(g.get("divergence_note") or "divergence flagged in CASE_GROUND_TRUTH.yaml governing_text")
    for c in ctx.get("causes_of_action") or []:
        if isinstance(c, dict) and c.get("enacted_by") and c.get("codified_at"):
            return True, f"{c.get('display_name') or c.get('id')}: enacted_by={c['enacted_by']} ≠ codified_at={c['codified_at']}"
    for r in ctx.get("conformity_results") or []:
        if isinstance(r, dict) and r.get("conforms") is False:
            return True, f"check_conformity: {r.get('subject') or r.get('lower_citation') or 'lower authority'} does not conform"
    return False, ""


def _is_pa_forum(ctx: dict) -> bool:
    if ctx.get("is_federal") is True:
        return False
    court = ctx.get("court")
    cid = str(ctx.get("case_id") or "")
    blob = " ".join([str(court or ""), cid, str(ctx.get("court_id") or "")]).lower()
    if cid.startswith("fed:"):
        return False
    return ("pa_" in blob or "pennsylvania" in blob or "phila" in blob or "pa_cp" in blob
            or ctx.get("is_federal") is False)


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────
def governing_text_gate(ctx: dict) -> dict:
    """Return the ctx keys the blocks consume. Never raises; default = omit."""
    res: dict[str, Any] = {
        "governing_text_emit": False,
        "governing_text_emit_federal": False,
        "governing_text_emit_pa": False,
        "governing_text_variant": "",
        "governing_text_reasons": [],
        "gt_count_cites": "",
        "gt_nonpositive_titles": "",
        "gt_act_name": "",
        "gt_enacted_by": "",
        "gt_divergence_note": "",
        "gt_divergence_sentence": "",
        "gt_ps_cites": "",
    }
    try:
        strings = _authority_strings(ctx)
        titles = usc_titles_cited(strings)
        nonpos = sorted(t for t in titles if t not in POSITIVE_LAW_TITLES)
        div, div_note = _divergence_flag(ctx)
        pa = _is_pa_forum(ctx)
        ps = ps_cites(strings) if pa else []

        reasons: list[str] = []
        if nonpos:
            reasons.append("(a) count rests on non-positive-law U.S.C. title(s): " + ", ".join(str(t) for t in nonpos))
        if div:
            reasons.append("(b) " + div_note)
        if ps:
            reasons.append("(c) PA forum; count rests on Purdon's (P.S.) cite(s): " + "; ".join(ps))
        if not reasons:
            return res

        variant = "pa" if (pa and (ps or div) and not nonpos) else ("federal" if (nonpos or not pa) else "pa")
        res.update({
            "governing_text_emit": True,
            "governing_text_emit_federal": variant == "federal",
            "governing_text_emit_pa": variant == "pa",
            "governing_text_variant": variant,
            "governing_text_reasons": reasons,
            "gt_nonpositive_titles": ", ".join(f"Title {t}" for t in nonpos),
            "gt_divergence_note": div_note,
            "gt_ps_cites": "; ".join(ps) if ps else "the compilation cited in the count",
        })
        if variant == "pa" and div:
            # 45 Pa.C.S. § 901 — verbatim [corpus:pa_cs_t45#3c96189ddc]; only when a divergence is flagged
            res["gt_divergence_sentence"] = (
                "Where the count turns on a regulation, the official text as published in the Pennsylvania "
                "Code or Pennsylvania Bulletin is, by statute, \"the only legal evidence of the valid and "
                "enforceable text of such regulation,\" 45 Pa.C.S. § 901, and a regulation can carry no "
                "meaning the enabling statute's words do not. "
            )
        elif variant == "pa":
            # single space, not "": filing_composer.hydrate_block renders "" as [MISSING: …]
            res["gt_divergence_sentence"] = " "
        # count cites actually pleaded (sorted, de-duplicated) for the block's opening sentence
        cites = []
        for t in sorted(titles):
            secs = sorted(titles[t], key=_sec_key)
            cites.append(f"{t} U.S.C. §§ {', '.join(secs)}" if len(secs) > 1 else
                         (f"{t} U.S.C. § {secs[0]}" if secs else f"Title {t}, U.S.C."))
        cites.extend(ps)
        res["gt_count_cites"] = "; ".join(cites)
        # enactment credit for the lead non-positive-law title (corpus-backed only)
        for t in nonpos:
            for sec in sorted(titles[t], key=_sec_key):
                key = f"{t}:{sec[:4]}"
                if key in ENACTMENT_CREDITS:
                    cr = ENACTMENT_CREDITS[key]
                    res["gt_act_name"] = cr["act"]
                    res["gt_enacted_by"] = cr["enacted_by"]
                    break
            if res["gt_enacted_by"]:
                break
        if not res["gt_enacted_by"]:
            # honest fallback — no invented Pub. L. number
            res["gt_act_name"] = "the Act"
            res["gt_enacted_by"] = "the Act as enacted and printed in the United States Statutes at Large (enactment credit: NOT IN CORPUS for this title — supply from the section's source credit)"
        return res
    except Exception as e:  # pragma: no cover — gate must never break a render
        res["governing_text_reasons"] = [f"gate error: {e}"]
        return res


def apply_governing_text_gate(ctx: dict) -> dict:
    """Mutate ctx in place with the gate outputs (used by build_context / build_filing_from_blocks)."""
    ctx.update(governing_text_gate(ctx))
    return ctx
