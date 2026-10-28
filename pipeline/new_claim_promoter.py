"""new_claim_promoter — the PICKUP step: turn deterministic email-scan candidates into queued
NEW CLAIMS. Deterministic (gate + court-routing); NO LLM, NO drafting here. m0 drafts later from
the queue (the only LLM step), honoring "deterministic until a new claim is found and grouped."

Flow:
  reads  output/email_violation_scan.json  (new_claim_candidate_groups)
  GATE   count >= MIN_EMAILS  AND  domain is a plausible adversary (has a dot, not noise/personal)
  ROUTE  m0_new_claim_engine.route_claim()  -> court, venue, required_forms, attachments
  WRITE  output/new_claims_queue.json  (status=pending_m0_draft) — the queue m0 drafts from
If nothing qualifies, that's fine — it writes an empty queue and says so.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, sys
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))
from pipeline import m0_new_claim_engine as m0nc

SCAN = ROOT / "output" / "email_violation_scan.json"
QUEUE = ROOT / "output" / "new_claims_queue.json"
MIN_EMAILS = int(next((a for a in sys.argv[1:] if a.isdigit()), "2"))

# obvious non-adversary / noise domains that should never become a claim
NOISE = ("buymeacoff", "morningbrew", "substack", "mailchimp", "linkedin", "meetup",
         "eventbrite", "medium.com", "gmail.com", "yahoo.com", "outlook.com", "?")

# credit-MONITORING / news / score apps — they REPORT on credit, they are not the entity that took
# the adverse action; they are never the defendant. Their "credit report" language is informational.
MONITORING = ("creditkarma", "creditsesame", "wallethub", "nerdwallet", "tomocredit", "creditrepair",
              "creditblueprint", "law.com", "rentpayment", "experian.com/p", "myfico", "smartcredit",
              "rocketmoney", "mint.com", "rentreporters", "self.inc", "kikoff")

# categories whose signal is REQUIRED boilerplate on routine statements (FCBA/FDCPA mini-Miranda is on
# every credit-card bill / collection notice) — high false-positive, so they HOLD for owner review
# rather than auto-queue. The genuine-claim outcomes (a denial / adverse action) auto-queue.
BOILERPLATE_PRONE = {"CREDIT_REPORTING", "CREDIT_CARD", "FCRA_FURNISHER"}

# violation category (litigation/consumer) -> claim_type for m0 court routing (best-effort; default
# consumer_protection so it still routes to a forum). Refine as real categories appear.
CATEGORY_TO_CLAIM = {
    "FCRA_FAILURE_TO_INVESTIGATE": "credit_reporting",
    "CREDIT_REPORTING": "credit_reporting",
    "FURNISHER": "fcra_furnisher",
    "FCRA_FURNISHER": "fcra_furnisher",
    "ADVERSE_ACTION_ECOA": "ecoa",            # credit denials -> ECOA (15 U.S.C. 1691)
    "DEBT_COLLECTION": "debt_collection",
    "CREDIT_CARD": "credit_card",
    "MEDICAL": "medical_billing",
    "MEDICAL_BILLING": "medical_billing",
    "INSURANCE": "insurance_bad_faith",
    "INSURANCE_BAD_FAITH": "insurance_bad_faith",
    "AGENCY_FAILURE_FACT": "consumer_protection",
}


def _is_adversary(domain: str) -> bool:
    d = (domain or "").lower()
    return "." in d and not any(n in d for n in NOISE) and not any(m in d for m in MONITORING)


def _load_groups():
    """Merge candidate groups from BOTH the litigation scan (email_violation_scan.json) and the
    consumer-claim scan (consumer_claim_scan.json). Either may be absent; that's fine."""
    out = []
    for p in (SCAN, ROOT / "output" / "consumer_claim_scan.json",
              ROOT / "output" / "m0_claim_scan.json"):   # m0-reasoning pass over regex-missed emails
        if p.exists():
            out += json.loads(p.read_text(encoding="utf-8")).get("new_claim_candidate_groups", [])
    return out


def promote():
    groups = _load_groups()
    if not groups:
        print("no scan candidates — run pipeline.email_violation_scan and/or pipeline.consumer_claim_scan first")
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE.write_text(json.dumps({"promoted": [], "skipped": [], "min_emails": MIN_EMAILS}, indent=2),
                         encoding="utf-8")
        return
    promoted, needs_review, skipped = [], [], []
    for g in groups:
        cat, domain, count = g.get("category", ""), g.get("domain", ""), g.get("count", 0)
        if count < MIN_EMAILS or not _is_adversary(domain):
            skipped.append({"category": cat, "domain": domain, "count": count,
                            "why": "below threshold" if count < MIN_EMAILS else "noise/monitoring/non-adversary"})
            continue
        claim = {
            "claim_type": CATEGORY_TO_CLAIM.get(cat, "consumer_protection"),
            "defendants": [{"name": domain, "does_business_in_pa": True}],   # citizenship verified at draft
            "amount": 0,
            "source": "consumer_claim_scan",
            "evidence_emails": g.get("examples", []),
            "violation_category": cat,
        }
        route = m0nc.route_claim(claim)
        rec = {
            "defendant_domain": domain, "violation_category": cat, "email_count": count,
            "claim_type": claim["claim_type"], "court": route["court"], "venue": route["venue"],
            "required_forms": route["required_forms"], "attachments_to_draft": route["claim_attachments_to_draft"],
            "removal_resistance": route["removal_resistance"], "evidence_emails": g.get("examples", []),
        }
        # genuine adverse-OUTCOME categories auto-queue for m0; boilerplate-prone HOLD for owner review
        # (the FCBA/FDCPA notice is on every routine statement — a human confirms it's a real harm first)
        if cat in BOILERPLATE_PRONE:
            rec["status"] = "needs_owner_review"
            rec["why_review"] = "signal is required statement boilerplate (FCBA/FDCPA notice) — confirm a real adverse event before drafting"
            needs_review.append(rec)
        else:
            rec["status"] = "pending_m0_draft"
            promoted.append(rec)
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps({"promoted": promoted, "needs_owner_review": needs_review,
                                 "skipped": skipped, "min_emails": MIN_EMAILS}, indent=2, default=str),
                     encoding="utf-8")
    print(f"promoted {len(promoted)} new claim(s) to the m0-draft queue (genuine adverse outcome, "
          f">= {MIN_EMAILS} emails, real adversary); {len(needs_review)} held for owner review; "
          f"skipped {len(skipped)} group(s)")
    for p in promoted:
        print(f"   QUEUE   {p['defendant_domain']:28} [{p['violation_category']}] -> {p['court']} ({p['email_count']})")
    for p in needs_review:
        print(f"   REVIEW  {p['defendant_domain']:28} [{p['violation_category']}] ({p['email_count']}) — {p['why_review'][:40]}")
    print(f"queue -> {QUEUE}  (pending_m0_draft = m0 drafts; needs_owner_review = confirm first)")


if __name__ == "__main__":
    promote()
