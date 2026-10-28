"""
scrapers/email_scraper.py — Gmail API email scraper for Beezle Legal AI
Scans inbox for agency correspondence, extracts violations, files into cases.

FEEDS: Agent 1 (Extractor) → triggers full reasoning chain

Usage:
    from scrapers.email_scraper import scan_inbox
    results = scan_inbox()  # scans last 30 days
    results = scan_inbox(days=7)  # scans last 7 days
    results = scan_inbox(sender="@cfpb.gov")  # specific agency
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, json, base64, re, time
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Optional

PROJECT_ROOT = _eroot()
CREDS_PATH = PROJECT_ROOT / "credentials.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"
OUTPUT_DIR = PROJECT_ROOT / "output" / "email_scans"
CASE_MAP_PATH = PROJECT_ROOT / "cases_index" / "case_map.json"

# ══════════════════════════════════════════════════════════
# AGENCY DETECTION — DB-driven, extensible
# ══════════════════════════════════════════════════════════
AGENCY_DOMAINS = {
    # Federal regulators
    "cfpb.gov": {"name": "CFPB", "full": "Consumer Financial Protection Bureau", "type": "federal", "statutes": ["15 USC 1681", "12 USC 5531"]},
    "consumerfinance.gov": {"name": "CFPB", "full": "Consumer Financial Protection Bureau", "type": "federal", "statutes": ["15 USC 1681", "12 USC 5531"]},
    "ftc.gov": {"name": "FTC", "full": "Federal Trade Commission", "type": "federal", "statutes": ["15 USC 45", "15 USC 1681s-2"]},
    "occ.gov": {"name": "OCC", "full": "Office of Comptroller of Currency", "type": "federal", "statutes": ["12 USC 1818"]},
    "fdic.gov": {"name": "FDIC", "full": "Federal Deposit Insurance Corporation", "type": "federal", "statutes": ["12 USC 1818"]},
    "sec.gov": {"name": "SEC", "full": "Securities and Exchange Commission", "type": "federal", "statutes": ["15 USC 78u"]},
    "irs.gov": {"name": "IRS", "full": "Internal Revenue Service", "type": "federal", "statutes": ["26 USC 7422"]},
    "treasury.gov": {"name": "Treasury", "full": "Department of Treasury", "type": "federal", "statutes": ["31 USC 3711"]},
    "hud.gov": {"name": "HUD", "full": "Housing and Urban Development", "type": "federal", "statutes": ["12 USC 2605"]},
    "doj.gov": {"name": "DOJ", "full": "Department of Justice", "type": "federal", "statutes": ["18 USC 241", "42 USC 1983"]},
    "usdoj.gov": {"name": "DOJ", "full": "Department of Justice", "type": "federal", "statutes": ["18 USC 241", "42 USC 1983"]},
    "fhfa.gov": {"name": "FHFA", "full": "Federal Housing Finance Agency", "type": "federal", "statutes": ["12 USC 4501"]},
    # State regulators
    "pa.gov": {"name": "PA-GOV", "full": "Pennsylvania State Government", "type": "state", "statutes": ["73 PS 201-3"]},
    "insurance.pa.gov": {"name": "PA-DOBI", "full": "PA Dept of Banking and Insurance", "type": "state", "statutes": ["40 PS 1171.5"]},
    "attorneygeneral.gov": {"name": "PA-AG", "full": "Pennsylvania Attorney General", "type": "state", "statutes": ["73 PS 201-9.2"]},
    # Industry
    "naic.org": {"name": "NAIC", "full": "National Assoc of Insurance Commissioners", "type": "industry", "statutes": []},
    "finra.org": {"name": "FINRA", "full": "Financial Industry Regulatory Authority", "type": "industry", "statutes": []},
    # Banks/creditors (detect responses to our disputes)
    "equifax.com": {"name": "Equifax", "full": "Equifax Information Services", "type": "cra", "statutes": ["15 USC 1681i"]},
    "experian.com": {"name": "Experian", "full": "Experian Information Solutions", "type": "cra", "statutes": ["15 USC 1681i"]},
    "transunion.com": {"name": "TransUnion", "full": "TransUnion LLC", "type": "cra", "statutes": ["15 USC 1681i"]},
    # Courts and legal services
    "uscourts.gov": {"name": "US-Courts", "full": "United States Courts", "type": "court", "statutes": []},
    "paed.uscourts.gov": {"name": "EDPA", "full": "Eastern District of Pennsylvania", "type": "court", "statutes": []},
    "pawd.uscourts.gov": {"name": "WDPA", "full": "Western District of Pennsylvania", "type": "court", "statutes": []},
    "pacourts.us": {"name": "PA-Courts", "full": "Pennsylvania Courts", "type": "court", "statutes": []},
    "courts.phila.gov": {"name": "Phila-CCP", "full": "Philadelphia Court of Common Pleas", "type": "court", "statutes": []},
    "ecf.paed.uscourts.gov": {"name": "ECF-EDPA", "full": "ECF Eastern District PA", "type": "court", "statutes": []},
    # Law firms and legal services (detect opposing counsel + service)
    "law.com": {"name": "LawFirm", "full": "Law Firm", "type": "legal", "statutes": []},
    "esq.com": {"name": "Attorney", "full": "Attorney", "type": "legal", "statutes": []},
    "legal.com": {"name": "LegalService", "full": "Legal Service", "type": "legal", "statutes": []},
    "lexisnexis.com": {"name": "LexisNexis", "full": "LexisNexis", "type": "legal_service", "statutes": []},
    "westlaw.com": {"name": "Westlaw", "full": "Westlaw", "type": "legal_service", "statutes": []},
    "pacer.gov": {"name": "PACER", "full": "Public Access to Court Electronic Records", "type": "court", "statutes": []},
    # Debt collectors
    "convergentusa.com": {"name": "Convergent", "full": "Convergent Outsourcing", "type": "debt_collector", "statutes": ["15 USC 1692"]},
    "portfoliorecovery.com": {"name": "PRA", "full": "Portfolio Recovery Associates", "type": "debt_collector", "statutes": ["15 USC 1692"]},
    "midlandcredit.com": {"name": "MCM", "full": "Midland Credit Management", "type": "debt_collector", "statutes": ["15 USC 1692"]},
    # Banks and financial
    "capitalone.com": {"name": "CapitalOne", "full": "Example Card Issuer Financial", "type": "bank", "statutes": ["12 USC 2605", "15 USC 1691"]},
    "chase.com": {"name": "Chase", "full": "Example Bank", "type": "bank", "statutes": ["12 USC 2605"]},
    "bankofamerica.com": {"name": "BofA", "full": "Bank of America", "type": "bank", "statutes": ["12 USC 2605"]},
    "wellsfargo.com": {"name": "WellsFargo", "full": "Wells Fargo", "type": "bank", "statutes": ["12 USC 2605"]},
    "citi.com": {"name": "Citi", "full": "Citibank", "type": "bank", "statutes": ["12 USC 2605"]},
    "discover.com": {"name": "Discover", "full": "Discover Financial", "type": "bank", "statutes": ["15 USC 1691"]},
    "amerisave.com": {"name": "Amerisave", "full": "Amerisave Mortgage Corporation", "type": "mortgage", "statutes": ["12 USC 2605"]},
    "statefarm.com": {"name": "StateFarm", "full": "State Farm Insurance", "type": "insurance", "statutes": ["42 PaCS 8371"]},
    # Insurance
    "allstate.com": {"name": "Allstate", "full": "Allstate Insurance", "type": "insurance", "statutes": ["42 PaCS 8371"]},
    "geico.com": {"name": "GEICO", "full": "GEICO Insurance", "type": "insurance", "statutes": ["42 PaCS 8371"]},
    "progressive.com": {"name": "Progressive", "full": "Progressive Insurance", "type": "insurance", "statutes": ["42 PaCS 8371"]},
    "dobi.nj.gov": {"name": "NJ-DOBI", "full": "NJ Dept of Banking and Insurance", "type": "state", "statutes": ["N.J.S.A. 17:29B-4"]},
}

# Violation keywords to extract from email bodies
VIOLATION_KEYWORDS = [
    "violation", "noncompliance", "deficiency", "failure to",
    "denied", "adverse action", "notice of", "complaint",
    "investigation", "enforcement", "consent order", "fine",
    "penalty", "sanction", "cease and desist", "corrective action",
    "unfair", "deceptive", "abusive", "discriminat",
    "inaccurate", "dispute", "reinvestigation", "furnisher",
]


def _get_gmail_service():
    """Authenticate and return Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {CREDS_PATH}. "
                    "Download from Google Cloud Console → APIs → Credentials → OAuth 2.0 Client ID"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# The user's own addresses: a self-forwarded scan ("for hermes", "Attached Scan") is paper service of a court/agency
# document and is as relevant as an agency sender — its attachments are the record. Class rule, no per-court list.
SELF_SENDERS = [x.strip().lower() for x in os.environ.get("BEEZLE_SELF_SENDERS", "filer.filer@planisware.com,you@example.com").split(",") if x.strip()]


def _detect_agency(email_from: str) -> Optional[dict]:
    """Match sender email to known agency domain."""
    email_lower = email_from.lower()
    if any(a in email_lower for a in SELF_SENDERS):
        return {"name": "SELF-SCAN", "full": "Self-forwarded scan (paper service)", "type": "scan", "statutes": [], "domain": "self", "sender": email_from}
    for domain, info in AGENCY_DOMAINS.items():
        if domain in email_lower:
            return {**info, "domain": domain, "sender": email_from}
    return None


def _extract_violations(subject: str, body: str, agency_info: dict) -> list[dict]:
    """Extract violation indicators from email content."""
    violations = []
    text = (subject + " " + body).lower()

    for kw in VIOLATION_KEYWORDS:
        if kw in text:
            # Find the sentence containing the keyword
            for sent in re.split(r'[.!?\n]', body):
                if kw in sent.lower():
                    violations.append({
                        "keyword": kw,
                        "context": sent.strip()[:200],
                        "agency": agency_info["name"],
                        "statutes": agency_info.get("statutes", []),
                    })
                    break  # one per keyword

    return violations


def _extract_case_references(body: str) -> list[str]:
    """Find case numbers, complaint IDs, reference numbers in email body."""
    patterns = [
        r'\b\d{1,2}:\d{2}-cv-\d{4,6}\b',           # federal case: 2:26-cv-00123
        r'\b\d{4}-CFPB-\d{4,8}\b',                   # CFPB complaint
        r'\b(?:Case|Ref|File|Complaint)\s*#?\s*[:.]?\s*(\S+)',  # generic ref
        r'\b[A-Z]{2,4}-\d{4,10}\b',                   # agency ref like OCC-20250001
    ]
    refs = []
    for p in patterns:
        for m in re.finditer(p, body, re.IGNORECASE):
            refs.append(m.group(0))
    return list(set(refs))


def _extract_deadlines(body: str) -> list[dict]:
    """Find deadline dates mentioned in email."""
    deadlines = []
    # Match patterns like "within 30 days", "by June 15, 2026", "deadline: 07/01/2026"
    day_pattern = r'within\s+(\d+)\s+(?:calendar\s+)?days'
    for m in re.finditer(day_pattern, body, re.IGNORECASE):
        deadlines.append({"type": "relative", "days": int(m.group(1)), "context": m.group(0)})

    date_pattern = r'\b(?:by|before|deadline|due)\s*:?\s*(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})'
    for m in re.finditer(date_pattern, body, re.IGNORECASE):
        deadlines.append({"type": "absolute", "date_str": m.group(1), "context": m.group(0)})

    return deadlines


def _match_to_case(violations: list, refs: list, agency_info: dict) -> Optional[str]:
    """Try to match email to an existing case in case_map.json."""
    if not CASE_MAP_PATH.exists():
        return None

    cm = json.loads(CASE_MAP_PATH.read_text(encoding="utf-8"))

    # Match by case reference number
    for ref in refs:
        for case_id, case_data in cm.items():
            if ref in case_id or ref in case_data.get("case_id", ""):
                return case_id

    # Match by defendant name in agency info
    agency_name = agency_info.get("name", "").lower()
    for case_id, case_data in cm.items():
        title = case_data.get("title", "").lower()
        for d in case_data.get("all_defendants", []):
            if agency_name in d.lower() or agency_name in title:
                return case_id

    return None


def scan_inbox(days: int = 30, sender: str = None, max_results: int = 0) -> dict:
    """
    Scan Gmail inbox for agency correspondence.

    Args:
        days: How many days back to scan
        sender: Optional specific domain filter (e.g. "@cfpb.gov")
        max_results: Maximum emails to process

    Returns:
        {
            "scan_date": "2026-04-24",
            "emails_scanned": 150,
            "agency_emails_found": 12,
            "violations_extracted": 8,
            "cases_matched": 3,
            "results": [...]
        }
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    service = _get_gmail_service()

    # Build search query
    after_date = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"after:{after_date}"
    if sender:
        query += f" from:{sender}"

    # Fetch ALL messages with pagination
    messages = []
    page_token = None
    page = 0
    while True:
        page += 1
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        results_list = service.users().messages().list(**kwargs).execute()
        batch = results_list.get("messages", [])
        messages.extend(batch)
        page_token = results_list.get("nextPageToken")
        print(f"  [page {page}] fetched {len(batch)} message IDs (total: {len(messages)})")
        if not page_token or (max_results and len(messages) >= max_results):
            break
    if max_results:
        messages = messages[:max_results]
    print(f"[email_scraper] Found {len(messages)} emails in last {days} days")

    scan_results = {
        "scan_date": datetime.now().isoformat()[:10],
        "query": query,
        "emails_scanned": 0,
        "agency_emails_found": 0,
        "violations_extracted": 0,
        "cases_matched": 0,
        "new_violations": 0,
        "results": [],
    }

    # PHASE 1: Scan headers only (metadata format — fast, no body downloaded)
    # This avoids fetching 51k full emails — only fetches headers
    print(f"[email_scraper] Phase 1: scanning {len(messages)} message headers...")
    matched_messages = []
    
    for i, msg_meta in enumerate(messages):
        if i > 0 and i % 200 == 0:
            print(f"  ... scanned {i}/{len(messages)} headers ({len(matched_messages)} matches so far)")
            time.sleep(0.5)  # rate limit courtesy
        
        try:
            msg_head = service.users().messages().get(
                userId="me", id=msg_meta["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()
        except Exception as e:
            continue
        
        headers = {h["name"].lower(): h["value"] for h in msg_head.get("payload", {}).get("headers", [])}
        sender_email = headers.get("from", "")
        subject = headers.get("subject", "")
        
        agency_info = _detect_agency(sender_email)
        
        # Also check subject keywords for unknown senders
        if not agency_info:
            legal_subject_kws = ["case no", "docket", "court order", "motion", "summons",
                "subpoena", "complaint", "notice of", "filing", "hearing", "judgment",
                "default", "discovery", "deposition", "settlement", "mediation",
                "attorney", "counsel", "law office", "esq", "legal notice",
                "adverse action", "credit report", "dispute", "collection",
                "account", "debt", "claim", "appeal", "remand"]
            subj_lower = subject.lower()
            matched_kw = None
            for kw in legal_subject_kws:
                if kw in subj_lower:
                    matched_kw = kw
                    break
            if matched_kw:
                domain_match = re.search(r"@([\w.-]+)", sender_email)
                sender_domain = domain_match.group(1) if domain_match else "unknown"
                agency_info = {
                    "name": sender_domain.split(".")[0].upper(),
                    "full": sender_email,
                    "type": "detected_legal",
                    "domain": sender_domain,
                    "sender": sender_email,
                    "statutes": [],
                    "match_reason": f"subject keyword: {matched_kw}",
                }
        
        if agency_info:
            matched_messages.append({
                "id": msg_meta["id"],
                "sender_email": sender_email,
                "subject": subject,
                "date": headers.get("date", ""),
                "agency_info": agency_info,
            })
    
    scan_results["emails_scanned"] = len(messages)
    print(f"[email_scraper] Phase 1 complete: {len(matched_messages)} relevant emails out of {len(messages)}")
    print(f"[email_scraper] Phase 2: fetching full content for {len(matched_messages)} matches...")
    
    # PHASE 2: Only fetch full body for matched emails
    for i, match in enumerate(matched_messages):
        if i > 0 and i % 50 == 0:
            print(f"  ... processed {i}/{len(matched_messages)} full emails")
            time.sleep(0.5)
        
        try:
            msg = service.users().messages().get(
                userId="me", id=match["id"], format="full"
            ).execute()
        except Exception as e:
            continue
        
        sender_email = match["sender_email"]
        subject = match["subject"]
        date_str = match["date"]
        agency_info = match["agency_info"]
        


        scan_results["agency_emails_found"] += 1

        # Extract body text
        body = ""
        payload = msg["payload"]
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                    body += base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        elif "body" in payload and "data" in payload["body"]:
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        # Extract attachments
        attachments = []
        attach_dir = OUTPUT_DIR / "attachments"
        attach_dir.mkdir(parents=True, exist_ok=True)
        
        def _get_attachments(parts, msg_id):
            """Recursively extract attachments from message parts."""
            found = []
            for part in parts:
                filename = part.get("filename", "")
                mime = part.get("mimeType", "")
                body_data = part.get("body", {})
                att_id = body_data.get("attachmentId")
                
                if att_id and filename:
                    # Download attachment
                    att = service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    file_data = base64.urlsafe_b64decode(att["data"])
                    
                    # Save with unique name
                    safe_name = re.sub(r'[^\w.\-]', '_', filename)
                    save_path = attach_dir / f"{msg_id[:8]}_{safe_name}"
                    save_path.write_bytes(file_data)
                    
                    found.append({
                        "filename": filename,
                        "mime_type": mime,
                        "size": len(file_data),
                        "saved_to": str(save_path),
                    })
                
                # Recurse into nested parts
                if "parts" in part:
                    found.extend(_get_attachments(part["parts"], msg_id))
            return found
        
        if "parts" in payload:
            attachments = _get_attachments(payload["parts"], msg_meta["id"])
            if attachments:
                print(f"    📎 {len(attachments)} attachments saved")

        # Extract violations
        violations = _extract_violations(subject, body, agency_info)
        case_refs = _extract_case_references(body)
        deadlines = _extract_deadlines(body)
        matched_case = _match_to_case(violations, case_refs, agency_info)

        if matched_case:
            scan_results["cases_matched"] += 1

        result = {
            "message_id": msg_meta["id"],
            "date": date_str,
            "from": sender_email,
            "subject": subject,
            "agency": agency_info,
            "violations": violations,
            "case_references": case_refs,
            "deadlines": deadlines,
            "matched_case": matched_case,
            "attachments": attachments,
            "body_preview": body[:500],
            "body_length": len(body),
        }

        scan_results["violations_extracted"] += len(violations)
        scan_results["results"].append(result)

        print(f"  [{agency_info['name']}] {subject[:60]} — {len(violations)} violations, case={matched_case or 'new'}")

    # Save scan results
    scan_file = OUTPUT_DIR / f"email_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    scan_file.write_text(json.dumps(scan_results, indent=2, default=str), encoding="utf-8")
    print(f"\n[email_scraper] Scan complete: {scan_results['agency_emails_found']} agency emails, {scan_results['violations_extracted']} violations")
    print(f"[email_scraper] Results saved: {scan_file}")

    return scan_results


def scan_and_chain(days: int = 30) -> list[dict]:
    """
    Scan inbox AND trigger reasoning chain for each violation found.
    This is the full Agent 1 → Agent 8 pipeline.
    """
    scan = scan_inbox(days=days)
    packets = []

    for result in scan.get("results", []):
        if not result["violations"]:
            continue

        # This is where the chain orchestrator will plug in
        # For now, structure the data for Agent 1 output format
        agent1_output = {
            "source": "email",
            "sender": result["from"],
            "date": result["date"],
            "subject": result["subject"],
            "agency": result["agency"]["name"],
            "agency_type": result["agency"]["type"],
            "violations": result["violations"],
            "case_references": result["case_references"],
            "deadlines": result["deadlines"],
            "matched_case": result["matched_case"],
            "statutes": result["agency"]["statutes"],
            "body_preview": result["body_preview"],
        }
        packets.append(agent1_output)

    print(f"\n[email_scraper] {len(packets)} violation packets ready for reasoning chain")
    return packets


# CLI entry point
if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    results = scan_inbox(days=days)
    print(json.dumps({k: v for k, v in results.items() if k != "results"}, indent=2))
