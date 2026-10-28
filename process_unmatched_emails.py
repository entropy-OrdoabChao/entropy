#!/usr/bin/env python3
"""
process_unmatched_emails.py - Process unmatched emails: match to cases, draft responses with scorecards.
"""
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from core import identity as _identity
import os
import sys
import json
import re
from pathlib import Path
from collections import Counter
from datetime import datetime

ROOT = (_eroot() / "obsidian_sync")
UNMATCHED_DIR = ROOT / "unmatched_emails"
CASES_DIR = ROOT / "cases"
OUTPUT_DIR = ROOT / "output"

SPAM_DOMAINS = ["mail.ms.aa.com", "cigna.com", "cigna", "ups.com", "fedex.com", "dhl",
                "amazon.com", "ebay.com", "gmail.com", "yahoo.com", "outlook.com",
                # newsletters / marketing that falsely tripped AGENCY_FAILURE_FACT
                "buymeacoff", "morningbrew", "substack", "mailchimp", "constantcontact",
                "sendgrid", "linkedin.com", "meetup.com", "eventbrite", "medium.com"]

# Regulator / agency sender domains -> the registry slug of that agency matter (generic, not case-specific).
AGENCY_ROUTING = {
    "cfpb.gov":           "cfpb_foia",
    "consumerfinance.gov": "cfpb_foia",
    "hud.gov":            "hud_foia",
    "dobi.nj":            "nj_doi",
    "nj.gov":             "nj_doi",
    "dobi.pa":            "pa_dobi",
    "insurance.pa.gov":   "pa_dobi",
}
# Court e-notice sender domains, keyed by registry case_court. A court domain routes to a case only
# when exactly one registered case sits in that court (otherwise the sender is ambiguous).
COURT_DOMAINS = {
    "PA_CP_PHILA": "courts.phila.gov",
}


def _load_registry():
    try:
        return json.loads((_eroot() / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _slug_for(cid, entry):
    s = (entry or {}).get("slug")
    if s:
        return str(s)
    c = str(cid)
    if c.startswith("fed:"):
        return "fed_" + c.split(":")[-1]
    return c.replace(":", "_", 1)


def _build_case_routing():
    """Sender-domain fragment -> case slug, derived from the case registry at runtime:
    every non-plaintiff party key of every registered case (plus any `email_domains` the entry
    lists) routes to that case; agency domains route to the agency matter; a court domain routes
    to the single case in that court."""
    routing = {}
    reg = _load_registry()
    by_court = {}
    for cid, e in reg.items():
        if not isinstance(e, dict):
            continue
        slug = _slug_for(cid, e)
        if e.get("case_court"):
            by_court.setdefault(str(e["case_court"]), []).append(slug)
        for dom in e.get("email_domains") or []:
            routing.setdefault(str(dom).lower(), slug)
        parties = [str(p).lower() for p in (e.get("parties") or [])]
        for i, p in enumerate(parties):
            if i == 0 or p == _identity.party_key() or _identity.is_me(p):
                continue                                    # parties[0] / the person = plaintiff
            compact = re.sub(r"[^a-z0-9]", "", p)
            if len(compact) < 4:
                continue
            routing.setdefault(compact, slug)
            routing.setdefault(p.replace("_", "-"), slug)
    for court, dom in COURT_DOMAINS.items():
        if len(by_court.get(court, [])) == 1:
            routing.setdefault(dom, by_court[court][0])
    for dom, slug in AGENCY_ROUTING.items():
        routing.setdefault(dom, slug)
    return routing


CASE_ROUTING = _build_case_routing()

def is_spam_email(email_path):
    """Return True for marketing/spam, False for legal correspondence.

    Order matters: SPAM_DOMAINS and subject keywords win over
    `agency_type: detected_legal` because the upstream tagger over-flags
    marketing emails as 'detected_legal' on words like 'claim'.
    """
    try:
        text = email_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    lower_text = text.lower()
    subject_spam = [
        "last chance", "claim your miles", "claim your reward",
        "view your account summary", "your account summary",
        "your monthly account summary", "promotional", "marketing",
        "unsubscribe at any time", "limited time offer",
        "% off your", "earn rewards",
    ]
    for kw in subject_spam:
        if kw in lower_text:
            return True

    sender_domain = ""
    if text.startswith("---"):
        fm_end = text.find("\n---", 3)
        if fm_end > 0:
            fm = text[3:fm_end]
            for line in fm.splitlines():
                if line.startswith("sender_domain:"):
                    sender_domain = line.split(":", 1)[1].strip().strip('"').lower()
                    break
            if "agency_type: MAIL" in fm or "agency_type: MARKETING" in fm:
                return True

    for spam in SPAM_DOMAINS:
        if spam and spam in sender_domain:
            return True

    return False

def extract_sender_domain(email_path):
    try:
        text = email_path.read_text(encoding="utf-8", errors="ignore")
    except:
        return None
    
    if text.startswith("---"):
        fm_end = text.find("\n---", 3)
        if fm_end > 0:
            fm = text[3:fm_end]
            for line in fm.splitlines():
                if line.startswith("sender_domain:"):
                    return line.split(":", 1)[1].strip().strip('"').lower()
    return None

def get_case_slug_from_domain(domain):
    if not domain:
        return None
    for pattern, slug in CASE_ROUTING.items():
        if pattern.lower() in domain.lower():
            return slug
    return None

def get_case_id_from_slug(slug):
    """slug -> canonical_id via the registry; unknown slugs pass through unchanged."""
    slug_to_case_id = {}
    for cid, e in _load_registry().items():
        if isinstance(e, dict):
            slug_to_case_id[_slug_for(cid, e)] = e.get("canonical_id") or cid
    return slug_to_case_id.get(slug, slug)

def get_case_info(case_slug):
    case_path = CASES_DIR / case_slug
    if not case_path.is_dir():
        return None
    markers = [
        case_path / "CASE_FACTS.md",
        case_path / "_case.md",
        case_path / "meta" / "CASE_GROUND_TRUTH.yaml",
    ]
    if any(m.exists() for m in markers):
        return {"slug": case_slug, "path": str(case_path)}
    return None

def write_response_to_case(email_path, response_path, case_slug, case_id):
    response_dir = Path(case_slug) / "emails"
    response_dir.mkdir(parents=True, exist_ok=True)
    original_name = email_path.stem
    response_name = f"{original_name}_response.md"
    response_path = response_dir / response_name
    
    text = email_path.read_text(encoding="utf-8", errors="ignore")
    if text.startswith("---"):
        fm_end = text.find("\n---", 3)
        fm = text[3:fm_end]
        body = text[fm_end + 4:]
        from_address = None
        for line in fm.splitlines():
            if line.startswith("from:"):
                from_address = line.split(":", 1)[1].strip()
                break
    else:
        fm = ""
        body = text
    
    response_content = f"""---
type: email_response
original_email: {email_path.name}
response_date: {datetime.now().isoformat()}
status: draft
case_id: {case_id}
case_folder: {case_slug}
---

# Email Response Draft

## Original Email
- **From:** {from_address if from_address else 'Unknown'}
- **Subject:** {original_name}

## Response
Thank you for your communication.

**Case Reference:** {case_id}

This matter is being handled as part of the above-referenced case. We are reviewing your communication and will respond accordingly.

## Next Steps
1. Review this draft for accuracy
2. Add any case-specific information
3. Finalize and send if appropriate

---
*This is a draft response. Please review before sending.*
"""
    response_path.write_text(response_content, encoding="utf-8")
    return response_path

def create_scorecard(email_path, case_id, case_slug):
    scorecard_dir = Path(case_slug)
    scorecard_path = scorecard_dir / "response_scorecard.md"
    scorecard_content = f"""# Response Scorecard

**Case ID:** {case_id}
**Case Folder:** {case_slug}
**Response Type:** Email Response

## Email Metadata
- **Original Email:** {email_path.name}
- **Status:** Draft
- **Created:** {datetime.now().isoformat()}

## Checklist
- [ ] Verify case assignment
- [ ] Review email context
- [ ] Check for legal requirements
- [ ] Add case-specific arguments
- [ ] Verify tone and language
- [ ] Review for completeness
- [ ] Final approval

## Notes
- This response should be consistent with case strategy
- Ensure all legal requirements are met
- Verify against case facts before sending

---
*Generated by process_unmatched_emails.py*
"""
    scorecard_path.write_text(scorecard_content, encoding="utf-8")
    return scorecard_path

def process_unmatched_emails():
    print("=" * 80)
    print("PROCESSING UNMATCHED EMAILS")
    print("=" * 80)
    print(f"Scanning directory: {UNMATCHED_DIR}")
    print()
    
    all_emails = list(UNMATCHED_DIR.glob("*.md"))
    total_emails = len(all_emails)
    print(f"Total emails found: {total_emails}")
    print()
    
    spam_count = 0
    matched_count = 0
    unmatched_count = 0
    stats = Counter()
    
    for email_path in sorted(all_emails):
        if "_reply" in email_path.name:
            continue
        
        if is_spam_email(email_path):
            spam_count += 1
            stats["spam_filtered"] += 1
            print(f"  [SPAM] {email_path.name}")
            continue
        
        domain = extract_sender_domain(email_path)
        case_slug = get_case_slug_from_domain(domain)
        
        if case_slug:
            matched_count += 1
            stats[case_slug] += 1
            
            case_info = get_case_info(case_slug)
            if case_info:
                case_id = get_case_id_from_slug(case_slug)
                response_path = Path(case_info["path"]) / "emails" / f"{email_path.stem}_response.md"
                write_response_to_case(email_path, response_path, case_slug, case_id)
                create_scorecard(email_path, case_id, case_slug)
                print(f"  [MATCHED] {email_path.name} -> {case_slug}")
            else:
                unmatched_count += 1
                stats["case_not_found"] += 1
                print(f"  [UNMATCHED] {email_path.name} -> {case_slug} (case not found)")
        else:
            unmatched_count += 1
            stats["no_domain_match"] += 1
            print(f"  [NO MATCH] {email_path.name} -> domain={domain}")
    
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total emails scanned: {total_emails}")
    print(f"Spam/Marketing filtered: {spam_count}")
    print(f"Matched to cases: {matched_count}")
    print(f"Unmatched: {unmatched_count}")
    print()
    print("Breakdown by case:")
    for case_slug, count in sorted(stats.items()):
        if count > 0 and "spam" not in str(case_slug).lower():
            print(f"  {case_slug}: {count}")
    print()
    
    return {"total": total_emails, "spam": spam_count, "matched": matched_count, 
            "unmatched": unmatched_count, "stats": dict(stats)}

if __name__ == "__main__":
    result = process_unmatched_emails()
    print()
    print("Processing complete!")
