#!/usr/bin/env python3
r"""
pipeline/filing_confidentiality.py

Tell m0 / the e-filing script WHAT goes WHERE in a Pennsylvania (FJD / CCP) e-filing: the CONFIDENTIAL
section vs the PUBLIC (non-confidential) section. Grounded in the PA Public Access Policy of the Unified
Judicial System (204 Pa. Code § 213; Pa.R.C.P. 205.6 confidential-document/confidential-information rules):
financial-source documents (IFP affidavits, bank/financial statements), SSNs, financial account numbers,
minors' identifiers, and medical/psychological records are filed as CONFIDENTIAL. A Notice of Appeal, motion,
brief, or praecipe is PUBLIC.

So the IFP petition + its Rule 240(b) financial affidavit + the bank statement (Exhibit A) => CONFIDENTIAL,
and the Notice of Appeal => PUBLIC. They must be uploaded SEPARATELY (never one merged PDF), or the financial
information is exposed on the public docket.

classify(name) -> {"section": "confidential"|"public", "reason": ...}. Never raises.
"""
from __future__ import annotations
import re

# markers that force CONFIDENTIAL placement (financial-source / protected personal info)
_CONFIDENTIAL = [
    (r"in[_ ]?forma[_ ]?pauperis|\bifp\b|proceed_ifp|pauperis", "IFP / financial-affidavit filing"),
    (r"240\(?b\)?|rule[_ ]?240", "Pa.R.C.P. 240(b) financial affidavit"),
    (r"affidavit.*financ|financ.*affidavit|affidavit_of_poverty|income.*affidavit", "financial affidavit"),
    (r"bank|statement.*\d{3,4}|financial[_ ]?record|account[_ ]?statement|chase|wells|pnc|checking",
     "bank / financial-account statement"),
    (r"\bssn\b|social[_ ]?security|tax[_ ]?return|w-?2|1099|pay[_ ]?stub", "SSN / tax / pay records"),
    (r"medical|psych|health[_ ]?record|hipaa", "medical/psychological record"),
    (r"minor|juvenile|child[_ ]?support.*financ", "minor / protected-person identifiers"),
    (r"confidential", "explicitly marked confidential"),
]
# markers that are affirmatively PUBLIC (never contain protected financial/personal info)
_PUBLIC = [
    (r"notice[_ ]?of[_ ]?appeal", "Notice of Appeal — public docket"),
    (r"\bbrief\b|appellate_brief|reply_brief", "brief — public"),
    (r"motion|petition_for|opposition|response|praecipe|complaint|answer|memorandum",
     "pleading/motion — public"),
    (r"docketing[_ ]?statement|cover[_ ]?sheet|certificate|proof[_ ]?of[_ ]?service|entry[_ ]?of[_ ]?appearance",
     "administrative — public"),
    (r"order|exhibit_index|scorecard", "order/index — public"),
]


# G10: patterns for PII / secrets / work-product that must NOT go out in a public filing (content scan).
_PII_CONTENT = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN pattern"),
    (r"(?i)\baccount\s*(?:no\.?|number|#)\s*:?\s*\d{5,}", "financial account number"),
    (r"\b(?:sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b",
     "API key / access token"),
    (r"(?i)\b(?:api[_-]?key|secret|password|token)\b\s*[:=]\s*\S{6,}", "credential assignment"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key material"),
    (r"\[\[(?!PAGEBREAK\]\])[^\]]+\]\]", "Obsidian wikilink — internal work-product leak"),  # [[PAGEBREAK]] is a render directive, not a leak
]


def scan_content_for_pii(text: str) -> list:
    """G10: scan OUTBOUND content (a draft/filing body) for PII, financial-account numbers, API keys/secrets,
    private keys, and internal work-product leaks BEFORE it is mailed or e-filed. Returns
    [{pattern, sample, at_line}] (empty = clean). A pre-send WARNING for the sender to surface; not an
    auto-redactor (redacting a filing silently is its own risk)."""
    out = []
    for pat, why in _PII_CONTENT:
        m = re.search(pat, text or "")
        if m:
            out.append({"pattern": why, "sample": m.group(0)[:40],
                        "at_line": (text or "")[:m.start()].count("\n") + 1})
    return out


def classify(name: str) -> dict:
    """Return the CCP e-filing section for a document, by filename or filing-type. CONFIDENTIAL wins on any
    financial/protected marker (fail safe: protecting info is the safer error)."""
    s = (name or "").lower()
    for pat, why in _CONFIDENTIAL:
        if re.search(pat, s):
            return {"section": "confidential", "reason": why}
    for pat, why in _PUBLIC:
        if re.search(pat, s):
            return {"section": "public", "reason": why}
    # unknown -> PUBLIC by default, but flag it so a human confirms (don't silently hide a public filing,
    # and don't silently expose an unrecognized one — surface it).
    return {"section": "public", "reason": "unclassified — VERIFY before filing (defaulted public)"}


def route(names: list) -> dict:
    """Group a list of documents into {'public': [...], 'confidential': [...]} for the two upload sections."""
    out = {"public": [], "confidential": []}
    for n in names:
        c = classify(n)
        out[c["section"]].append({"name": n, "reason": c["reason"]})
    return out


if __name__ == "__main__":
    import sys, json
    docs = sys.argv[1:] or [
        "notice_of_appeal_superior_court_FILING.pdf",
        "00_petition_to_proceed_ifp_FILING.pdf",
        "20260615-statements-8907-.pdf",
        "appellate_brief_superior_court_FILING.pdf",
    ]
    print(json.dumps(route(docs), indent=2))
