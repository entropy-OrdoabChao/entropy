"""m1 lane: ORIGINAL PROCEEDING (mandamus / prohibition — FRAP 21, 28 U.S.C. § 1651).

An original proceeding is neither a complaint (no counts, no notice to defend) nor a
rebuttal (there is no opposing brief to answer). Its anatomy is fixed by FRAP 21(a)(2)(B):
relief sought · issues presented · facts necessary to understand the issues · reasons
the writ should issue. The stub (m0) supplies the caption, the reason headings, the
record-fact seed (incl. the prior-mailings ledger from pipeline.mailing_backfill) and
the relief. This lane fills each reason heading from the gated relay cells
(output/research/rooms_ab/*_cells.jsonl — every cell GROUNDED by the proof room).

Division of labour (user-locked): deterministic = GUARDRAIL (candidate pool, cite
pinning, fail-closed), LLM = COMPREHENSION (which cells answer this heading, and the
prose). Every authority in the output must already exist in a cell; anything else is
stripped and the section falls back to the deterministic listing.
"""
import json, re, sys, urllib.request
from pathlib import Path

STOP = set("the a an of to in on for and or is are was were be by with that this from as at it its "
           "not no any all under which who whom whose than then there their".split())


def _tok(s):
    return {w for w in re.findall(r"[a-z0-9§]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def _split_sections(md):
    """-> (preamble, [(h2_title, body)]) ; h2 = '## ' headings."""
    parts = re.split(r"(?m)^## ", md)
    pre = parts[0]
    secs = []
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        secs.append((title.strip(), body))
    return pre, secs


def _reason_headings(body):
    return [h.strip() for h in re.findall(r"(?m)^### (.+)$", body)]


NEG_RX = re.compile(r"\b(does not lie|is not reviewable|not a legal determination|factual observation|routine factual|"
                    r"does not (?:create|affect|impair|divest)|properly reviewed on direct appeal|no (?:clear|indisputable) right|"
                    r"mandamus (?:is|was) (?:not|un)available|waived|forfeited|moot(?:ed)?|harmless)\b", re.I)


def posture(cell, llm_url=None, party="Petitioner", relief="the writ should issue"):
    """FOR / AGAINST / NEUTRAL relative to the filing party. Deterministic guard first (a
    proposition that negates the ground is never FOR); LLM comprehension for the rest.
    A verification room's honest NO is a skeptic card, not a ground — it must never be
    printed under a reason heading."""
    if cell.get("posture") in ("FOR", "AGAINST", "NEUTRAL"):
        return cell["posture"]
    txt = (cell.get("proposition") or "") + " " + (cell.get("finding") or "")
    if NEG_RX.search(txt):
        return "AGAINST"
    if not llm_url:
        return "NEUTRAL"
    try:
        q = (f"A party ({party}) asks a court of appeals for this relief: {relief}. Does the following finding "
             f"HELP that party (FOR), HURT that party (AGAINST), or neither (NEUTRAL)? Answer one word.\n\nFINDING: {txt[:900]}")
        body = json.dumps({"model": _server_model(llm_url), "temperature": 0, "max_tokens": 5,
                           "chat_template_kwargs": {"enable_thinking": False},
                           "messages": [{"role": "user", "content": q}]}).encode()
        req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            a = (json.loads(r.read())["choices"][0]["message"].get("content") or "").strip().upper()
        return "FOR" if a.startswith("FOR") else "AGAINST" if a.startswith("AGAINST") else "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def _candidates(heading, cells, n=10):
    ht = _tok(heading)
    scored = []
    for c in cells:
        ct = _tok(c.get("title", "")) | _tok(c.get("proposition", "")) | _tok(c.get("finding", ""))
        ov = len(ht & ct)
        if ov:
            scored.append((ov + (0.5 if c.get("authorities") else 0), c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:n]]


CITE_RX = (r"\d+\s+(?:U\.S\.|F\.\d?d|F\. ?Supp\.(?: \d?d)?|S\. ?Ct\.|Pa\.(?: Super\.| Cmwlth\.)?|A\.\d?d|L\. ?Ed\.(?: \d?d)?)\s+\d+"
           r"|\d+\s+U\.S\.C\.\s+§+\s*[\w().-]+"
           r"|§+\s*\d[\w().-]*")   # bare section refs as cells write them ("§ 1681s-2(b)")


def _cites_in(cells, extra_text=""):
    """Allowed cite set = every cite the GROUNDED cells carry: structured authorities plus
    any cite written in the cells' own proposition/finding text (the proof room gated the
    whole cell). The heading's own cites are allowed too (m0 wrote the heading)."""
    out = set()
    for c in cells:
        for a in c.get("authorities", []) or []:
            if a.get("cite"):
                out.add(a["cite"].strip())
        for k in ("proposition", "finding", "title", "claim"):
            out |= set(re.findall(CITE_RX, c.get(k) or ""))
    out |= set(re.findall(CITE_RX, extra_text or ""))
    return {re.sub(r"[.,;]$", "", x).strip() for x in out}


def _deterministic_section(heading, cands):
    """Fail-closed text: proposition + finding + pinned quote, one paragraph per cell."""
    paras = []
    for i, c in enumerate(cands, 1):
        p = (c.get("proposition") or c.get("finding") or "").strip()
        f = (c.get("finding") or "").strip()
        txt = p if p == f or not f else f"{p} {f}"
        for a in c.get("authorities", []) or []:
            if a.get("cite"):
                q = (a.get("quote") or "").strip()
                txt += f' {a["cite"]}' + (f' ("{q[:240]}")' if q else "") + "."
        paras.append(f"{i}. {txt}")
    return "\n\n".join(paras)


def _server_model(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=10) as r:
            return json.loads(r.read())["data"][0]["id"]
    except Exception:
        return "default"


def _llm_section(heading, cands, llm_url, court_note):
    allowed = _cites_in(cands, heading)
    evidence = "\n".join(
        f"- CELL {i}: {c.get('proposition','')} | FINDING: {c.get('finding','')}"
        + ("".join(f" | AUTHORITY: {a.get('cite')} — \"{(a.get('quote') or '')[:300]}\"" for a in (c.get('authorities') or []) if a.get('cite')))
        for i, c in enumerate(cands, 1))
    prompt = (
        "You are drafting one section of a petition for a writ of mandamus under Fed. R. App. P. 21 to the "
        "United States Court of Appeals for the Third Circuit, for a petitioner appearing sui juris. "
        f"SECTION HEADING: {heading}\n\n"
        "Write 3–7 numbered paragraphs that prove the heading from the CELLS below and nothing else. "
        "Rules: (1) cite ONLY authorities that appear verbatim in a CELL's AUTHORITY field, using the exact cite string; "
        "(2) quote only the quoted text given; (3) no conclusory labels — state what the record shows and what the "
        "rule says, let the court draw the conclusion; (4) never write 'pro se', 'frivolous' or 'sovereign'; "
        "(5) plain confident appellate prose, no headings, no preamble, markdown numbered paragraphs only. "
        f"{court_note}\n\nCELLS:\n{evidence}")
    # Reasoning models (qwen3) spend the whole budget thinking and return content=None:
    # thinking off, larger budget, and read reasoning_content as the fallback — never .strip() None.
    body = json.dumps({"model": _server_model(llm_url), "temperature": 0.3, "max_tokens": 4000,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        msg = json.loads(r.read())["choices"][0]["message"]
        out = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not out:
        raise ValueError("empty completion")
    out = re.sub(r"<think>.*?</think>\s*", "", out, flags=re.S)
    # GUARDRAIL: every cite-looking token must be one of the allowed cites
    found = {re.sub(r"[.,;]$", "", f).strip() for f in re.findall(CITE_RX, out)}
    def _ok(f):
        toks = f.split()
        sec = toks[-1].split("(")[0].lstrip("§")
        if toks[0].startswith("§"):
            return any(sec in a for a in allowed)
        return any(toks[0] in a and sec in a for a in allowed) or ("§" in f and any(sec in a and "§" in a for a in allowed))
    bad = [f for f in found if not _ok(f)]
    unquoted = re.sub(r"[\"“][^\"”]{0,400}[\"”]", "", out)   # the record may be quoted; self-labels may not
    if bad or re.search(r"\bpro se\b|frivolous|sovereign", unquoted, re.I):
        raise ValueError(f"unpinned cites or banned words: {bad[:3]}")
    return out


def assemble(stub_path, cells_path, case_id, court, out_path, llm_url=None):
    stub = Path(stub_path).read_text(encoding="utf-8")
    cells = [json.loads(l) for l in Path(cells_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    cells = [c for c in cells if (c.get("status") or "").upper() == "GROUNDED"]
    # POSTURE GATE: tag every cell once; AGAINST cells go to the skeptic ledger, never a heading.
    relief_m = re.search(r"(?ms)^## RELIEF\b.*?\n(.*?)(?=\n## |\Z)", stub)
    relief_txt = (relief_m.group(1).strip()[:600] if relief_m else "the writ should issue")
    against = []
    for c in cells:
        c["posture"] = posture(c, llm_url, relief=relief_txt)
        if c["posture"] == "AGAINST":
            against.append(c)
    cells = [c for c in cells if c["posture"] == "FOR"]
    if against:
        led = Path(out_path).with_name("SKEPTIC_LEDGER_against_cells.jsonl")
        led.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in against), encoding="utf-8")
        print(f"  [posture] {len(against)} AGAINST cell(s) routed to {led.name}; {len(cells)} FOR cell(s) remain", flush=True)
    pre, secs = _split_sections(stub)
    court_note = "This Court is bound by its own precedent and the Supreme Court; cite the rule text first."
    out_secs, stats = [], {"headings": 0, "llm": 0, "fallback": 0, "cells_used": 0}
    for title, body in secs:
        if title.upper().startswith(("ARGUMENT", "REASONS")):
            heads = _reason_headings(body)
            intro = body.split("### ")[0].strip()
            new = (intro + "\n\n") if intro else ""
            for h in heads:
                cands = _candidates(h, cells)
                stats["headings"] += 1; stats["cells_used"] += len(cands)
                text = ""
                if llm_url and cands:
                    try:
                        text = _llm_section(h, cands, llm_url, court_note); stats["llm"] += 1
                    except Exception as e:
                        print(f"  [orig-proc] LLM rejected for '{h[:50]}': {e} — deterministic", flush=True)
                if not text:
                    # Deterministic fallback prints the cells' own propositions only (no prose): the
                    # guardrail, not a substitute for comprehension. Empty pool → flagged for m0.
                    text = _deterministic_section(h, cands) if cands else "[NO FOR-POSTURE GROUNDED CELL — route heading to m0 ANALYSIS / NOT IN CORPUS]"
                    stats["fallback"] += 1
                new += f"### {h}\n\n{text}\n\n"
            out_secs.append(("ARGUMENT", "\n" + new))
        elif title.upper().startswith("ISSUES PRESENTED") and not body.strip():
            arg = next((b for t, b in secs if t.upper().startswith(("ARGUMENT", "REASONS"))), "")
            issues = [re.sub(r"^[IVX]+\.\s*", "", h) for h in _reason_headings(arg)]
            out_secs.append((title, "\n" + "\n\n".join(f"{i}. Whether {s[0].lower() + s[1:]}." for i, s in enumerate(issues, 1)) + "\n"))
        else:
            out_secs.append((title, body))
    md = pre + "".join(f"## {t}\n{b}" for t, b in out_secs)
    Path(out_path).write_text(md, encoding="utf-8")
    stats["words"] = len(md.split())
    return stats
