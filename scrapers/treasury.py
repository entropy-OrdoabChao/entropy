"""
scrapers/treasury.py — Treasury / OCC / Financial Regulatory Scraper

Fetches:
  1. Treasury Directives — policy from treasury.gov
  2. Treasury Regulations (31 CFR) — via eCFR
  3. OCC Bulletins — supervisory guidance from occ.gov
  4. OCC Comptroller's Handbook — examination booklets (PDF)
  5. OCC Interpretive Letters — legal interpretations
  6. OCC Enforcement Actions — formal orders against banks
  7. FinCEN guidance — anti-money laundering (31 CFR 1010-1099)

Authority hierarchy for financial regulation:
  Constitution → Congressional statute (12/15/26/31 USC) →
  Agency regulation (CFR) → Agency guidance (bulletins/handbook) →
  Interpretive letters → Enforcement actions

Usage:
    from scrapers.treasury import fetch_occ_bulletin, fetch_occ_handbook
    from scrapers.treasury import fetch_treasury_cfr, fetch_fincen_reg

    result = fetch_occ_bulletin(2024, 15)
    result = fetch_occ_handbook("compliance-mgmt-systems")
    result = fetch_treasury_cfr(part="1010")
"""

import re
import logging
import time
from typing import Optional, List
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from scrapers._base import save_primary_source, html_to_text, safe_id

log = logging.getLogger("scraper_treasury")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BeezleLegalAI/1.0"}
RATE_LIMIT = 1.5


def _get(url: str, timeout: int = 30) -> Optional[str]:
    """GET with rate limiting and error handling."""
    if not requests:
        log.error("requests library not available")
        return None
    try:
        time.sleep(RATE_LIMIT)
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
        log.warning(f"HTTP {r.status_code} for {url}")
        return None
    except Exception as e:
        log.warning(f"Request failed: {url}: {e}")
        return None


def _get_bytes(url: str, timeout: int = 60) -> Optional[bytes]:
    """GET binary content (for PDFs)."""
    if not requests:
        return None
    try:
        time.sleep(RATE_LIMIT)
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.content
        log.warning(f"HTTP {r.status_code} for {url}")
        return None
    except Exception as e:
        log.warning(f"Request failed: {url}: {e}")
        return None


def _extract_pdf_text(pdf_bytes: bytes, label: str = "") -> str:
    """Extract text from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
        import io
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        text = "\n\n".join(text_parts)
        log.info(f"PDF {label}: {len(pdf.pages)} pages, {len(text)} chars")
        return text
    except Exception as e:
        log.warning(f"PDF extraction failed for {label}: {e}")
        return ""


# ─── Treasury Regulations (31 CFR) ──────────────────────────────────

def fetch_treasury_cfr(part: str = None, section: str = None) -> dict:
    """Fetch Treasury Department regulations from 31 CFR.

    Key parts:
      Part 10    — Practice before the IRS (Circular 230)
      Part 103   — Financial recordkeeping (legacy BSA)
      Part 1010  — General FinCEN provisions
      Part 1020  — Bank BSA requirements
      Part 1022  — MSB requirements
      Part 1023  — Broker-dealer requirements
      Part 1024  — Mutual fund requirements
      Part 1025  — Insurance company requirements
      Part 1026  — Futures commission merchant requirements
    """
    from scrapers.fed_register import fetch_cfr
    result = fetch_cfr(title_num=31, part=part, section=section)

    # Enrich metadata
    path = result.get("path", "")
    if path and Path(path).exists():
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if "agency:" not in text and text.startswith("---"):
            text = text.replace("---\n", "---\nagency: Treasury\n", 1)
            Path(path).write_text(text, encoding="utf-8")

    return result


def fetch_circular_230() -> dict:
    """Fetch Circular 230 — regulations governing practice before the IRS.

    31 CFR Part 10. Critical for tax practitioner liability.
    """
    return fetch_treasury_cfr(part="10")


# ─── OCC Bulletins ───────────────────────────────────────────────────

def fetch_occ_bulletin(year: int, number: int) -> dict:
    """Fetch an OCC Bulletin from occ.gov.

    OCC Bulletins are the primary vehicle for communicating supervisory
    guidance to national banks and federal savings associations.

    URL pattern: occ.gov/news-issuances/bulletins/{year}/bulletin-{year}-{number}.html
    """
    url = f"https://www.occ.gov/news-issuances/bulletins/{year}/bulletin-{year}-{number}.html"

    html = _get(url)
    if not html:
        return {"error": f"OCC Bulletin {year}-{number} not found", "url": url}

    text = html_to_text(html)
    if len(text) < 100:
        return {"error": f"OCC Bulletin {year}-{number}: insufficient content"}

    # Extract title
    title_match = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.IGNORECASE | re.DOTALL)
    title = html_to_text(title_match.group(1)).strip() if title_match else f"OCC Bulletin {year}-{number}"

    source_id = f"occ_bulletin_{year}_{number}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation/federal",
        title=f"OCC Bulletin {year}-{number}: {title[:100]}",
        text=text,
        url=url,
        extra_frontmatter={
            "document_type": "occ_bulletin",
            "year": year,
            "number": number,
            "agency": "OCC",
            "parent_agency": "Treasury",
            "authority_level": "agency_guidance",
        },
    )

    log.info(f"Fetched OCC Bulletin {year}-{number}: {len(text)} chars")
    return {**result, "url": url, "text_length": len(text)}


def scrape_occ_bulletins(year: int, max_bulletins: int = 60) -> list:
    """Scrape all OCC bulletins for a given year."""
    results = []
    consecutive_misses = 0

    for num in range(1, max_bulletins + 1):
        r = fetch_occ_bulletin(year, num)
        if r.get("error"):
            consecutive_misses += 1
            if consecutive_misses >= 5:
                break
        else:
            consecutive_misses = 0
            results.append(r)

    log.info(f"OCC Bulletins {year}: {len(results)} fetched")
    return results


# ─── OCC Comptroller's Handbook ──────────────────────────────────────

# Key handbook booklets and their URL slugs
OCC_HANDBOOK_BOOKLETS = {
    "bank-supervision-process": "Bank Supervision Process",
    "community-bank-supervision": "Community Bank Supervision",
    "large-bank-supervision": "Large Bank Supervision",
    "corporate-risk-governance": "Corporate and Risk Governance",
    "compliance-mgmt-systems": "Compliance Management Systems",
    "model-risk-management": "Model Risk Management",
    "credit-risk": "Rating Credit Risk",
    "interest-rate-risk": "Interest Rate Risk",
    "liquidity": "Liquidity",
    "operational-risk": "Operational Risk",
    "consumer-compliance": "Consumer Compliance",
    "truth-in-lending": "Truth in Lending (Regulation Z)",
    "fair-lending": "Fair Lending",
    "real-estate-lending": "Real Estate Lending Standards",
    "asset-securitization": "Asset Securitization",
    "bank-secrecy-act": "Bank Secrecy Act (BSA)",
    "deposit-related-credit": "Deposit-Related Credit",
    "fiduciary-activities": "Fiduciary Activities",
    "custody-services": "Custody Services",
    "country-risk": "Country Risk Management",
}


def fetch_occ_handbook(booklet_slug: str) -> dict:
    """Fetch an OCC Comptroller's Handbook booklet.

    Tries HTML index page first, then PDF download.

    Args:
        booklet_slug: URL slug from OCC_HANDBOOK_BOOKLETS
    """
    booklet_name = OCC_HANDBOOK_BOOKLETS.get(booklet_slug, booklet_slug.replace("-", " ").title())

    # Try HTML index page
    index_url = (
        f"https://www.occ.gov/publications-and-resources/publications/"
        f"comptrollers-handbook/files/{booklet_slug}/index-{booklet_slug}.html"
    )
    html = _get(index_url)
    text = ""

    if html:
        text = html_to_text(html)

    # If HTML is thin, try the PDF
    if len(text) < 500:
        pdf_url = (
            f"https://www.occ.gov/publications-and-resources/publications/"
            f"comptrollers-handbook/files/{booklet_slug}/pub-ch-{booklet_slug}.pdf"
        )
        pdf_bytes = _get_bytes(pdf_url)
        if pdf_bytes:
            text = _extract_pdf_text(pdf_bytes, f"OCC Handbook: {booklet_name}")
            if text:
                log.info(f"Got PDF for OCC Handbook: {booklet_name}")

    if len(text) < 100:
        return {"error": f"OCC Handbook '{booklet_slug}' not found"}

    source_id = f"occ_handbook_{safe_id(booklet_slug)}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation/federal",
        title=f"Comptroller's Handbook: {booklet_name}",
        text=text,
        url=index_url,
        extra_frontmatter={
            "document_type": "occ_handbook",
            "booklet": booklet_slug,
            "agency": "OCC",
            "parent_agency": "Treasury",
            "authority_level": "agency_guidance",
        },
    )

    log.info(f"Fetched OCC Handbook: {booklet_name}: {len(text)} chars")
    return {**result, "url": index_url, "text_length": len(text)}


def scrape_occ_handbook(booklets: list = None) -> dict:
    """Scrape multiple OCC handbook booklets.

    Default: all booklets in OCC_HANDBOOK_BOOKLETS.
    """
    if booklets is None:
        booklets = list(OCC_HANDBOOK_BOOKLETS.keys())

    summary = {"booklets_scraped": [], "total": 0, "errors": []}

    for slug in booklets:
        r = fetch_occ_handbook(slug)
        if r.get("error"):
            summary["errors"].append(f"{slug}: {r['error']}")
        else:
            summary["booklets_scraped"].append(slug)
            summary["total"] += 1

    log.info(f"OCC Handbook: {summary['total']}/{len(booklets)} booklets fetched")
    return summary


# ─── OCC Enforcement Actions ─────────────────────────────────────────

def fetch_occ_enforcement(year: int = 2024, action_type: str = "all") -> dict:
    """Fetch OCC enforcement actions listing page.

    The OCC publishes formal enforcement actions (consent orders,
    cease and desist orders, civil money penalties) on their website.

    URL: occ.gov/topics/laws-and-regulations/enforcement-actions/enforcement-action-list/index-enforcement-action-list.html
    """
    url = f"https://www.occ.gov/topics/laws-and-regulations/enforcement-actions/enforcement-action-list/index-enforcement-action-list.html"

    html = _get(url)
    if not html:
        return {"error": "Failed to fetch OCC enforcement actions", "url": url}

    text = html_to_text(html)
    if len(text) < 100:
        return {"error": "OCC enforcement actions: insufficient content"}

    source_id = f"occ_enforcement_index_{year}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="enforcement/federal",
        title=f"OCC Enforcement Actions Index ({year})",
        text=text,
        url=url,
        extra_frontmatter={
            "document_type": "enforcement_index",
            "year": year,
            "agency": "OCC",
            "authority_level": "enforcement_action",
        },
    )

    return {**result, "url": url, "text_length": len(text)}


# ─── OCC Interpretive Letters ─────────────────────────────────────────

def fetch_occ_interpretive_letter(number: int) -> dict:
    """Fetch an OCC interpretive letter.

    OCC interpretive letters provide legal interpretations of banking
    statutes and regulations. Historically numbered sequentially.
    """
    url = f"https://www.occ.gov/topics/laws-and-regulations/interpretive-letters/int-letter-{number}.html"

    html = _get(url)
    if not html:
        return {"error": f"OCC Interpretive Letter #{number} not found", "url": url}

    text = html_to_text(html)
    if len(text) < 100:
        return {"error": f"OCC Letter #{number}: insufficient content"}

    source_id = f"occ_interpretive_{number}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation/federal",
        title=f"OCC Interpretive Letter #{number}",
        text=text,
        url=url,
        extra_frontmatter={
            "document_type": "occ_interpretive_letter",
            "number": number,
            "agency": "OCC",
            "authority_level": "agency_interpretation",
        },
    )

    return {**result, "url": url, "text_length": len(text)}


# ─── FinCEN (Financial Crimes Enforcement Network) ───────────────────

def fetch_fincen_reg(part: str = None) -> dict:
    """Fetch FinCEN regulations from 31 CFR Chapter X (Parts 1010-1099).

    Key parts:
      1010 — General provisions (definitions, reporting, recordkeeping)
      1020 — Rules for banks
      1022 — Rules for MSBs
      1023 — Rules for brokers/dealers in securities
      1024 — Rules for mutual funds
      1025 — Rules for insurance companies
      1026 — Rules for futures commission merchants
    """
    if part is None:
        part = "1010"
    return fetch_treasury_cfr(part=part)


# ─── Banking Statutes (12 USC) ───────────────────────────────────────

def fetch_banking_statute(section: str = None) -> dict:
    """Fetch from Title 12 USC — Banks and Banking.

    Key sections:
      § 1     — Office of Comptroller of the Currency
      § 24    — Corporate powers of national banks
      § 93    — Violation of provisions of chapter (penalties)
      § 371   — Real estate loans
      § 1813  — FDIC definitions
      § 1818  — Termination of status as insured depository institution
      § 1831o — Prompt corrective action
      § 3102  — Federal branches/agencies of foreign banks
    """
    from scrapers.fed_register import fetch_usc
    return fetch_usc(title_num=12, section=section)


def fetch_securities_statute(section: str = None) -> dict:
    """Fetch from Title 15 USC — Commerce and Trade (includes securities law).

    Key sections:
      § 77a-77aa  — Securities Act of 1933
      § 78a-78pp  — Securities Exchange Act of 1934
      § 80a       — Investment Company Act of 1940
      § 80b       — Investment Advisers Act of 1940
      § 1681      — FCRA
      § 1692      — FDCPA
    """
    from scrapers.fed_register import fetch_usc
    return fetch_usc(title_num=15, section=section)


# ─── Unified Fetch ───────────────────────────────────────────────────

def fetch(source_type: str = "occ_bulletin", **kwargs) -> dict:
    """Unified fetch interface matching the stub signature.

    Args:
        source_type: "occ_bulletin", "occ_handbook", "occ_enforcement",
                     "occ_letter", "treasury_cfr", "circular_230",
                     "fincen", "banking_statute", "securities_statute"
        **kwargs: passed to the specific fetch function
    """
    dispatch = {
        "occ_bulletin": lambda: fetch_occ_bulletin(
            kwargs.get("year", 2024), kwargs.get("number", 1)
        ),
        "occ_handbook": lambda: fetch_occ_handbook(
            kwargs.get("booklet", "bank-supervision-process")
        ),
        "occ_enforcement": lambda: fetch_occ_enforcement(
            kwargs.get("year", 2024)
        ),
        "occ_letter": lambda: fetch_occ_interpretive_letter(
            kwargs.get("number", 1)
        ),
        "treasury_cfr": lambda: fetch_treasury_cfr(
            part=kwargs.get("part"), section=kwargs.get("section")
        ),
        "circular_230": lambda: fetch_circular_230(),
        "fincen": lambda: fetch_fincen_reg(part=kwargs.get("part")),
        "banking_statute": lambda: fetch_banking_statute(
            section=kwargs.get("section")
        ),
        "securities_statute": lambda: fetch_securities_statute(
            section=kwargs.get("section")
        ),
    }

    handler = dispatch.get(source_type)
    if handler:
        return handler()
    return {"error": f"Unknown treasury source type: {source_type}"}
