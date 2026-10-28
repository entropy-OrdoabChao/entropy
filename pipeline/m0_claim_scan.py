# -*- coding: utf-8 -*-
"""
m0_claim_scan — AGNOSTIC duty-breach discovery over the email backlog.

It does NOT know any institution names and carries NO keyword list. For each ORIGINAL inbound email it
asks m0 one question, reasoned from first principles: does this evidence a BREACH OF A DUTY owed to the
recipient (or the recipient pursuing one)? Every actor owes duties — a bank (Reg E/UDAAP/FCRA), a
mortgage servicer (RESPA), an insurer (good faith), a dentist/doctor (complete care, informed consent,
standard of care), a crypto custodian (safekeeping/withdrawal), a merchant (goods/services paid for), a
government agency (statutory duty). m0 discovers the obligor and the duty; it is never hard-coded.

Two stages, on purpose:
  scan(batch)  — DETECTION. Fast, batched, resumable. No corpus load. Reasons subject+sender+attachments
                 +body (body is often empty; the substance is the subject/sender/attachment). No case-domain
                 skip and no regex skip, so it DISCOVERS violations even from senders routed to a case.
  ground(...)  — GROUNDING. Loads the 3.05M-chunk corpus ONCE and, for every discovered duty, attaches the
                 duty's legal source [corpus] (prefer_authority) + the Obsidian link-graph context. This is
                 what makes each find grounded/linked rather than asserted.

Deterministic-safe: if m0 is unavailable nothing is flagged. Emits output/m0_claim_scan.json.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "obsidian_sync" / "beezle_managed"))
import process_unmatched_emails as P

OUT = ROOT / "output" / "m0_claim_scan.json"
DONE = ROOT / "output" / "m0_claim_scan_done.json"

# Prefer the FULL-body store (gmail_full_pull backfill) once it exists — the unmatched_emails/*.md store
# only ever held a 500-char body PREVIEW, so reasoning over it saw a stub, not the email. emails_full/*.md
# carry the complete body fetched live from Gmail.
_FULL_DIR = ROOT / "obsidian_sync" / "emails_full"
EMAILS_DIR = _FULL_DIR if (_FULL_DIR.exists() and next(_FULL_DIR.glob("*.md"), None)) else P.UNMATCHED_DIR

# AGNOSTIC — no institution names, no keyword gate. m0 reasons about DUTIES from first principles.
_PROMPT = (
    "You are Beezle. DISCOVER whether this email evidences a BREACH OF A DUTY owed to the recipient (the "
    "person who received it), or the recipient asserting/pursuing such a breach. Reason from first "
    "principles about DUTIES -- do NOT rely on any list of companies or industries; discover the obligor "
    "yourself. Every party that deals with a person owes duties: statutory, regulatory, contractual, "
    "fiduciary, or professional. The breadth is unlimited (ILLUSTRATIONS ONLY, never a checklist): a "
    "bank/creditor owes Reg E / Reg CC / UDAAP / FCRA / ECOA duties; a mortgage servicer owes RESPA and "
    "chain-of-title duties; an insurer owes good-faith claim handling and the duty to defend/pay; a "
    "healthcare or dental provider owes a duty to complete care, informed consent, and the standard of "
    "care; a custodian/exchange owes safekeeping and timely withdrawal; a merchant or service provider "
    "owes the goods or services paid for; a government agency owes its statutory duty. If ANY duty is owed "
    "to the recipient and this email shows it may have been breached (or the recipient is pursuing that "
    "breach), answer YES -- regardless of who the obligor is or what industry they are in.\n"
    "Answer NO ONLY for: pure marketing/offers, rewards, a routine statement, a receipt or shipping "
    "notice, a one-time verification / 2FA code, a password reset, a routine sign-in alert, or an action "
    "the recipient themselves requested with no grievance.\n"
    "The body may be EMPTY -- then reason from the SUBJECT + SENDER + ATTACHMENT names alone.\n"
    "Output EXACTLY one line:\n"
    "DUTY: <yes|no> | OBLIGOR: <who owed the duty, taken from the email> | DUTY_TYPE: <short_snake_case> | "
    "BREACH: <what was done or omitted, one clause> | FORUM: <the regulator or court that would hear it, or "
    "none> | WHY: <one sentence grounded in the email>\n\n")

_LINE = re.compile(
    r"DUTY:\s*(yes|no)\s*\|\s*OBLIGOR:\s*([^|]*?)\s*\|\s*DUTY_TYPE:\s*([a-z_]+)\s*\|\s*"
    r"BREACH:\s*([^|]*?)\s*\|\s*FORUM:\s*([^|]*?)\s*\|\s*WHY:\s*(.+)", re.I)


def _fm(t: str, key: str) -> str:
    for line in t.splitlines():
        if line.strip().lower().startswith(key):
            return line.split(":", 1)[-1].strip().strip('"').strip()
    return ""


def _real_body(t: str) -> str:
    body = t.split("## Body", 1)[-1] if "## Body" in t else t
    body = body.split("## Detected Statutes", 1)[0]
    return re.sub(r"[ \t]+", " ", body).strip()


def _attach_names(t: str) -> str:
    return " ; ".join(re.findall(r'"filename":\s*"([^"]+)"', t))[:200]


_UNSUB = re.compile(r"unsubscribe|manage (your )?(email )?preferences|view (this )?(email )?in (your )?browser|"
                    r"you.{0,6}re receiving this|you received this (email|because)|update your preferences|"
                    r"no longer (wish|want) to receive|opt.?out|email preferences", re.I)
_MATTER_REF = re.compile(r"\b(account|claim|case|policy|complaint|reference|confirmation|appointment|loan|"
                         r"ticket|dispute|invoice|member|file|order)\b[^.\n]{0,20}[:#]?\s*[0-9][0-9A-Za-z\-]{3,}", re.I)


def _worth_reasoning(subject: str, body: str) -> bool:
    """AGNOSTIC feasibility gate (no institution/name hardcoding). Reason anything with a RELATIONSHIP
    signal — a reply/forward thread, or a reference to a specific matter/account/claim/appointment number;
    otherwise reason it UNLESS it carries broadcast-marketing markers (unsubscribe / 'you're receiving
    this') with no such signal. Keeps the dentist, the credit denial, every real obligor; drops newsletters."""
    subl = (subject or "").lower()
    if subl.startswith(("re:", "fwd:", "fw:", "re :", "fwd :")):
        return True
    if _MATTER_REF.search(body or ""):
        return True
    if _UNSUB.search(body or ""):
        return False
    return True


def _call_llm(prompt: str, max_tokens: int = 160, timeout: int = 90) -> str:
    """The ONE function that POSTs to the model (seat-able: an Entropy organ seat swaps this and nothing else)."""
    from pipeline.case_facts_refresh import _call_direct
    return _call_direct(prompt, max_tokens=max_tokens, timeout=timeout)


def discover(subject: str, sender: str, attach: str, body: str):
    """Organ entry point: one email → duty-breach dict or None. Parsing stays the organ's own."""
    return _m0(subject, sender, attach, body)


def _m0(subject: str, sender: str, attach: str, body: str):
    """Ask m0 to discover a duty-breach. Returns dict or None (unavailable/garbled)."""
    payload = ("SUBJECT: %s\nFROM: %s\nATTACHMENTS: %s\n\n=== BODY (may be empty) ===\n%s"
               % (subject or "(none)", sender or "(none)", attach or "(none)", (body or "")[:3500]))
    resp = _call_llm(_PROMPT + payload, max_tokens=160, timeout=90)
    m = _LINE.search(resp or "")
    if not m:
        return None
    return {"duty": m.group(1).lower() == "yes", "obligor": m.group(2).strip()[:60],
            "duty_type": m.group(3).lower(), "breach": m.group(4).strip()[:200],
            "forum": m.group(5).strip()[:60], "why": m.group(6).strip()[:200]}


def scan(batch: int = 200) -> dict:
    """DETECTION stage. Reason the next `batch` un-done ORIGINAL emails for a duty-breach, agnostically.
    Holds flow_lock so the learning keeper yields the local-gpu (else the sweep + learning OOM the box)."""
    from collections import defaultdict
    try:
        from pipeline import flow_lock
        flow_lock.acquire("email-sweep")
    except Exception:
        pass
    try:
        done = set(json.loads(DONE.read_text(encoding="utf-8"))) if DONE.exists() else set()
    except Exception:
        done = set()
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    except Exception:
        prev = {}
    found = prev.get("claims_found", [])
    # Originals only. The ~12.8k "_reply.md" files are auto-generated templated DRAFTS ("Thank you for
    # your correspondence") that only echo the subject -> no independent content. Everything else is read.
    files = [f for f in EMAILS_DIR.iterdir()
             if f.is_file() and f.suffix == ".md" and not f.name.endswith("_reply.md")]
    reasoned = remaining = 0
    new_this_run = []
    for f in files:
        if f.name in done:
            continue
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
            subject = _fm(t, "subject:")
            body = _real_body(t)
            # AGNOSTIC feasibility gate: drop pure broadcast marketing (unsubscribe-footer, no relationship
            # signal) before it costs an m0 call. Keeps replies, matter/reference emails, the dentist, etc.
            if not _worth_reasoning(subject, body):
                done.add(f.name); continue
            # NO regex skip and NO case-domain skip: a bank/insurer routed to a case can still carry an
            # INDEPENDENT duty-breach (a Reg E denial, a closure) that the case never worked. Discover it.
            if reasoned >= batch:
                remaining += 1; continue
            sender = _fm(t, "from:") or _fm(t, "sender:")
            domain = _fm(t, "sender_domain:") or (P.extract_sender_domain(f) or "?")
            r = _m0(subject, sender, _attach_names(t), body)
            reasoned += 1; done.add(f.name)
            if r and r["duty"]:
                rec = {"file": f.name, "domain": domain, "obligor": r["obligor"],
                       # keep "category" key for downstream compatibility (== duty_type)
                       "category": r["duty_type"], "duty_type": r["duty_type"], "breach": r["breach"],
                       "forum": r["forum"], "why": r["why"], "subject": subject[:140],
                       "grounded": False}
                found.append(rec); new_this_run.append(rec)
        except Exception:
            done.add(f.name); continue
    DONE.write_text(json.dumps(sorted(done)), encoding="utf-8")
    grp = defaultdict(list)
    for c in found:
        grp[(c.get("obligor", "?"), c["duty_type"])].append(c["file"])
    groups = sorted(({"obligor": k[0], "duty_type": k[1], "count": len(v), "examples": v[:12]}
                     for k, v in grp.items()), key=lambda x: -x["count"])
    report = {"reasoned_this_run": reasoned, "batch": batch, "remaining_to_reason": remaining,
              "total_handled": len(done), "claims_found": found, "duties_found": found,
              "duty_groups": groups}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("piece: m0-reasoned %d email(s) this run; %d still to sweep; %d duty-breach(es) discovered "
          "(total handled %d)" % (reasoned, remaining, len(found), len(done)))
    for c in new_this_run:
        print("NEWDUTY: %s | %s | %s | %s" % (c.get("obligor", "?"), c["duty_type"], c.get("forum", "?"),
                                              c["why"][:80]))
    try:
        from pipeline import flow_lock; flow_lock.release()
    except Exception:
        pass
    return report


def ground(limit: int = 0) -> dict:
    """GROUNDING stage. Load the corpus ONCE and attach each discovered duty's legal source [corpus]
    (prefer_authority) + the Obsidian link-graph context. Idempotent: only grounds records not yet grounded."""
    from pipeline.drafter_grounding import _corpus_hits
    try:
        from pipeline.connected_notes import graph_context
    except Exception:
        graph_context = None
    data = json.loads(OUT.read_text(encoding="utf-8"))
    duties = data.get("claims_found", [])
    todo = [c for c in duties if not c.get("grounded")]
    if limit:
        todo = todo[:limit]
    n = 0
    for c in todo:
        q = ("%s %s %s legal duty statute regulation standard of care"
             % (c.get("obligor", ""), c.get("duty_type", ""), c.get("breach", "")))[:400]
        hits = _corpus_hits(q, k=4)
        cited = [{"text": (h.get("text") or "").strip()[:320], "score": round(h.get("score") or 0, 3),
                  "cite": h.get("citation") or h.get("source") or h.get("id")}
                 for h in hits if (h.get("score") or 0) >= 0.42 and (h.get("text") or "").strip()]
        c["corpus_basis"] = cited[:3]
        c["obsidian"] = ""
        if graph_context is not None:
            try:
                c["obsidian"] = graph_context(q, k=3, max_chars=600) or ""
            except Exception:
                c["obsidian"] = ""
        c["grounded"] = True
        n += 1
    data["claims_found"] = duties
    data["duties_found"] = duties
    data["grounded_count"] = sum(1 for c in duties if c.get("grounded"))
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("grounded %d duty-breach(es); %d of %d now carry corpus+obsidian basis"
          % (n, data["grounded_count"], len(duties)))
    return data


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "ground":
        ground(int(next((a for a in args[1:] if a.isdigit()), "0")))
    else:
        scan(int(next((a for a in args if a.isdigit()), "200")))
