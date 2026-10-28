"""consumer_claim_scan — DETERMINISTIC detection of NEW consumer-law CLAIMS in the email backlog
(credit denials / adverse action, FCRA, FDCPA, TILA/FCBA, medical billing, insurance bad-faith).

This is the consumer-harm complement to email_violation_scan.py. That scanner uses the LITIGATION
classifier (how an opposing filing misbehaves) and is blind to a "your application was denied" email.
THIS scanner reads the email text for consumer-claim SIGNALS and emits the SAME report shape, so
new_claim_promoter picks both up with no change, routes the forum (m0_new_claim_engine), and m0
drafts the complaint from the queue — the only LLM step ("deterministic until a claim is found").

For each email in obsidian_sync/unmatched_emails:
  1. drop marketing/noise (process_unmatched_emails.is_spam_email)
  2. regex-match consumer-claim SIGNALS -> claim category (NO LLM)
  3. route the sender domain to an active case; if it routes, it's evidence, not a NEW claim
NEW-claim candidate = non-spam email with a consumer-claim signal that does NOT route to an active
case, grouped by (category, sender domain). Writes output/consumer_claim_scan.json. Drafts nothing.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "obsidian_sync" / "beezle_managed"))

import process_unmatched_emails as P

OUT = ROOT / "output" / "consumer_claim_scan.json"
LIMIT = int(next((a for a in sys.argv[1:] if a.isdigit()), "0")) or None

# category -> claim-signal patterns. A category fires if ANY pattern matches (case-insensitive).
# Patterns are written tight to avoid over-firing on marketing ("apply now!") — they key on the
# *outcome* language a real adverse-action / collection / denial notice uses.
SIGNALS = {
    "ADVERSE_ACTION_ECOA": [
        r"\badverse action\b",
        r"\b(application|request|account|loan|credit line)\b.{0,30}\b(was |is |been |has been )?(denied|declined|rejected|not approved|turned down)\b",
        r"\b(unable|decline[ds]?)\b.{0,20}\b(approve|extend|offer)\b",
        r"\breasons? for (the |our |this )?(denial|decision|adverse action)\b",
        r"\b(credit|your application)\b.{0,15}\b(was )?denied\b",
        r"\becoa\b", r"\b15 u\.?s\.?c\.? *1691\b", r"\bequal credit opportunity\b",
    ],
    "CREDIT_REPORTING": [
        r"\bcredit report\b", r"\bcredit bureau\b", r"\bconsumer report\b",
        r"\b1681\b", r"\bfcra\b", r"\breinvestigat", r"\breinserted\b",
        r"\bresults? of your dispute\b", r"\bdispute\b.{0,30}\b(inaccura|error|incorrect|not mine)\b",
    ],
    "FCRA_FURNISHER": [
        r"\bdata furnisher\b", r"\bfurnished?\b.{0,25}\b(bureau|agenc|equifax|experian|transunion)\b",
        r"\breported (to|the)\b.{0,20}\b(bureau|credit agenc)\b",
    ],
    "DEBT_COLLECTION": [
        r"\bdebt collector\b", r"\bthis is an attempt to collect\b", r"\b1692\b", r"\bfdcpa\b",
        r"\bvalidate (the |this )?debt\b", r"\bdebt validation\b",
        r"\b(amount|balance) (owed|due|outstanding)\b", r"\bcollection (agency|account|notice)\b",
        r"\byour account .{0,20}placed (for |in )?collection\b",
    ],
    "CREDIT_CARD": [
        r"\bbilling error\b", r"\b1666\b", r"\bfcba\b", r"\bfair credit billing\b",
        r"\bdispute (the |this |a )?(charge|transaction)\b",
        r"\bunauthorized (charge|transaction|use)\b", r"\bchargeback\b",
    ],
    "MEDICAL_BILLING": [
        r"\bmedical bill\b", r"\bitemized (bill|statement)\b", r"\bexplanation of benefits\b", r"\beob\b",
        r"\bpatient (statement|balance|responsibility)\b", r"\bhospital (bill|account)\b",
    ],
    "INSURANCE_BAD_FAITH": [
        r"\bclaim\b.{0,20}\b(was |is |been |has been )?(denied|denial)\b",
        r"\b8371\b", r"\bbad faith\b", r"\bdenial of (your )?claim\b",
        r"\bcoverage\b.{0,15}\b(was )?(denied|declined|terminated)\b",
    ],
}
_COMPILED = {cat: [re.compile(p, re.I) for p in pats] for cat, pats in SIGNALS.items()}


def _categories(text: str) -> list:
    """All consumer-claim categories whose signal fires in this text (most-specific first kept)."""
    return [cat for cat, rxs in _COMPILED.items() if any(r.search(text) for r in rxs)]


def scan():
    files = [f for f in P.UNMATCHED_DIR.iterdir() if f.is_file() and f.suffix == ".md"]
    if LIMIT:
        files = files[:LIMIT]
    by_cat = Counter()
    spam = matched_existing = no_signal = 0
    new_candidates = defaultdict(list)            # (category, domain) -> [names]
    for f in files:
        try:
            if P.is_spam_email(f):
                spam += 1
                continue
            body = f.read_text(encoding="utf-8", errors="ignore")
            cats = _categories(body[:8000])
            if not cats:
                no_signal += 1
                continue
            domain = P.extract_sender_domain(f) or "?"
            slug = P.get_case_slug_from_domain(domain)
            for cat in cats:
                by_cat[cat] += 1
            if slug:
                matched_existing += 1             # evidence for an active case, not a NEW claim
            else:
                # one candidate per email keyed on its most-specific (first) category
                new_candidates[(cats[0], domain)].append(f.name)
        except Exception:
            by_cat["(error)"] += 1
    groups = sorted(({"category": c, "domain": d, "count": len(n), "examples": n[:12]}
                     for (c, d), n in new_candidates.items()), key=lambda x: -x["count"])
    report = {
        "total_scanned": len(files), "spam_dropped": spam, "no_consumer_signal": no_signal,
        "signal_matched_to_active_case": matched_existing,
        "new_claim_candidate_count": sum(len(n) for n in new_candidates.values()),
        "consumer_signals_by_category": dict(by_cat.most_common()),
        "new_claim_candidate_groups": groups,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"scanned={report['total_scanned']} spam={spam} no-signal={no_signal} "
          f"signal->active_case={matched_existing} NEW-claim-candidates={report['new_claim_candidate_count']}")
    print("consumer-claim signals by category:")
    for c, n in by_cat.most_common(12):
        print(f"   {c}: {n}")
    print("\nNEW consumer-claim candidate groups (category | domain | count):")
    for g in groups[:15]:
        print(f"   {g['category']:22} {g['domain']:30} {g['count']}")
    if not groups:
        print("   (none — no new consumer claim surfaced from the unmatched email backlog)")
    print(f"\nreport -> {OUT}  (feed to: python -m pipeline.new_claim_promoter)")
    return report


if __name__ == "__main__":
    scan()
