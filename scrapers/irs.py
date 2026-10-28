"""
scrapers/irs.py — IRS / Internal Revenue Scraper

Fetches:
  1. Internal Revenue Manual (IRM) — HTML from irs.gov/irm
  2. Treasury Regulations (26 CFR) — via eCFR API (reuses fed_register.fetch_cfr)
  3. Revenue Rulings — from irs.gov
  4. Revenue Procedures — from irs.gov
  5. IRS Publications — key taxpayer-facing guidance
  6. Internal Revenue Code (26 USC) — via fed_register.fetch_usc

All content saved to Obsidian via save_primary_source() and
ingested to ChromaDB. Every node is traceable to its source URL.

Usage:
    from scrapers.irs import fetch_irm, fetch_treasury_reg, fetch_rev_ruling

    # Fetch IRM Part 5, Chapter 1, Section 9
    result = fetch_irm(part=5, chapter=1, section=9)

    # Fetch 26 CFR § 1.61-1
    result = fetch_treasury_reg(section="1.61-1")

    # Bulk scrape key IRM parts
    results = scrape_irm_parts([1, 4, 5, 8, 20, 21, 25])
"""

import re
import logging
import time
from typing import Optional
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from scrapers._base import save_primary_source, html_to_text, safe_id

log = logging.getLogger("scraper_irs")

BASE_IRM_URL = "https://www.irs.gov/irm/part{part}/irm_{part_pad}-{chapter_pad}-{section_pad}"
BASE_IRM_PART_URL = "https://www.irs.gov/irm/part{part}"
BASE_REV_RULING_URL = "https://www.irs.gov/pub/irs-irbs"
BASE_IRS_PUB_URL = "https://www.irs.gov/publications"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BeezleLegalAI/1.0"}
RATE_LIMIT = 1.5  # seconds between requests


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


# ─── IRM (Internal Revenue Manual) ──────────────────────────────────

def fetch_irm(part: int, chapter: int, section: int) -> dict:
    """Fetch a single IRM section from irs.gov.

    IRM numbering: Part.Chapter.Section (e.g., 5.1.9 = Part 5, Chapter 1, Section 9)
    URL pattern: irs.gov/irm/part5/irm_05-001-009

    Returns dict with path, title, url, text_length, or error.
    """
    part_pad = f"{part:02d}"
    chapter_pad = f"{chapter:03d}"
    section_pad = f"{section:03d}"

    url = BASE_IRM_URL.format(
        part=part,
        part_pad=part_pad,
        chapter_pad=chapter_pad,
        section_pad=section_pad,
    )

    html = _get(url)
    if not html:
        return {"error": f"Failed to fetch IRM {part}.{chapter}.{section}", "url": url}

    # Extract main content
    text = html_to_text(html)

    # Extract title from page
    title_match = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.IGNORECASE | re.DOTALL)
    title = html_to_text(title_match.group(1)) if title_match else f"IRM {part}.{chapter}.{section}"
    title = title.strip()[:200]

    if len(text) < 50:
        return {"error": f"IRM {part}.{chapter}.{section}: insufficient content ({len(text)} chars)", "url": url}

    source_id = f"irm_{part}_{chapter}_{section}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation/federal",
        title=f"IRM {part}.{chapter}.{section} — {title}",
        text=text,
        url=url,
        section=f"IRM Part {part}, Chapter {chapter}, Section {section}",
        extra_frontmatter={
            "irm_part": part,
            "irm_chapter": chapter,
            "irm_section": section,
            "agency": "IRS",
            "parent_agency": "Treasury",
        },
    )

    log.info(f"Fetched IRM {part}.{chapter}.{section}: {len(text)} chars")
    return {**result, "url": url, "text_length": len(text)}


def fetch_irm_chapter(part: int, chapter: int, max_sections: int = 50) -> list:
    """Fetch all sections of an IRM chapter.

    Tries sections 1 through max_sections, stops after 3 consecutive misses.
    """
    results = []
    consecutive_misses = 0

    for section in range(1, max_sections + 1):
        r = fetch_irm(part, chapter, section)
        if r.get("error"):
            consecutive_misses += 1
            if consecutive_misses >= 3:
                break
        else:
            consecutive_misses = 0
            results.append(r)

    log.info(f"IRM Part {part}, Chapter {chapter}: {len(results)} sections fetched")
    return results


def scrape_irm_parts(parts: list = None) -> dict:
    """Bulk scrape key IRM parts.

    Default parts are the most litigation-relevant:
      Part 1  — Organization, Finance, Management
      Part 4  — Examining Process
      Part 5  — Collecting Process
      Part 8  — Appeals
      Part 20 — Penalties & Interest
      Part 21 — Customer Account Services
      Part 25 — Special Topics (fraud, bankruptcy, innocent spouse)
    """
    if parts is None:
        parts = [1, 4, 5, 8, 20, 21, 25]

    summary = {"parts_scraped": [], "total_sections": 0, "errors": []}

    for part in parts:
        part_results = []
        for chapter in range(1, 30):  # most parts have <30 chapters
            chapter_results = fetch_irm_chapter(part, chapter, max_sections=30)
            if not chapter_results:
                break  # no more chapters in this part
            part_results.extend(chapter_results)

        summary["parts_scraped"].append({
            "part": part,
            "sections_fetched": len(part_results),
        })
        summary["total_sections"] += len(part_results)
        log.info(f"IRM Part {part}: {len(part_results)} total sections")

    return summary


# ─── Treasury Regulations (26 CFR) ──────────────────────────────────

def fetch_treasury_reg(section: str = None, part: str = None) -> dict:
    """Fetch Treasury Regulations from 26 CFR.

    Uses the existing fed_register.fetch_cfr() for eCFR access.

    Args:
        section: specific section (e.g., "1.61-1" for income definition)
        part: CFR part number (e.g., "1" for income tax regs)

    Returns dict with path, title, text_length, or error.
    """
    from scrapers.fed_register import fetch_cfr

    result = fetch_cfr(title_num=26, part=part, section=section)
    if result.get("error"):
        return result

    # Add IRS-specific metadata
    path = result.get("path", "")
    if path:
        from pathlib import Path as _P
        node_path = _P(path)
        if node_path.exists():
            text = node_path.read_text(encoding="utf-8", errors="replace")
            # Inject agency metadata into frontmatter
            if "agency:" not in text and text.startswith("---"):
                text = text.replace("---\n", "---\nagency: IRS/Treasury\n", 1)
                node_path.write_text(text, encoding="utf-8")

    log.info(f"Fetched 26 CFR {part or ''} § {section or ''}: {result.get('text_length', '?')} chars")
    return result


def scrape_treasury_regs(parts: list = None) -> dict:
    """Bulk scrape key Treasury Regulation parts.

    Default parts:
      Part 1   — Income Tax (the big one)
      Part 20  — Estate Tax
      Part 25  — Gift Tax
      Part 31  — Employment Tax
      Part 301 — Procedure and Administration
      Part 601 — Statement of Procedural Rules
    """
    if parts is None:
        parts = ["1", "20", "25", "31", "301", "601"]

    summary = {"parts_scraped": [], "total": 0, "errors": []}

    for part in parts:
        result = fetch_treasury_reg(part=str(part))
        if result.get("error"):
            summary["errors"].append(f"26 CFR Part {part}: {result['error']}")
        else:
            summary["parts_scraped"].append({"part": part, "chars": result.get("text_length", 0)})
            summary["total"] += 1

    return summary


# ─── Revenue Rulings & Procedures ───────────────────────────────────

def fetch_rev_ruling(year: int, number: int) -> dict:
    """Fetch a Revenue Ruling from IRS.

    Revenue Rulings are published in the Internal Revenue Bulletin (IRB).
    URL pattern varies by year.
    """
    # Try multiple URL patterns
    urls = [
        f"https://www.irs.gov/pub/irs-irbs/irb{year % 100:02d}-{number:02d}.pdf",
        f"https://www.irs.gov/irb/{year}-{number:02d}_IRB",
    ]

    for url in urls:
        html = _get(url)
        if html:
            text = html_to_text(html)
            if len(text) > 100:
                source_id = f"rev_ruling_{year}_{number}"
                result = save_primary_source(
                    source_id=source_id,
                    jurisdiction="federal",
                    source_type="regulation/federal",
                    title=f"Revenue Ruling {year}-{number}",
                    text=text,
                    url=url,
                    extra_frontmatter={
                        "document_type": "revenue_ruling",
                        "year": year,
                        "number": number,
                        "agency": "IRS",
                    },
                )
                log.info(f"Fetched Rev. Rul. {year}-{number}: {len(text)} chars")
                return {**result, "url": url, "text_length": len(text)}

    return {"error": f"Rev. Rul. {year}-{number} not found"}


def fetch_rev_procedure(year: int, number: int) -> dict:
    """Fetch a Revenue Procedure from IRS."""
    url = f"https://www.irs.gov/irb/{year}-{number:02d}_IRB"

    html = _get(url)
    if not html:
        return {"error": f"Rev. Proc. {year}-{number} not found", "url": url}

    text = html_to_text(html)
    if len(text) < 100:
        return {"error": f"Rev. Proc. {year}-{number}: insufficient content"}

    source_id = f"rev_proc_{year}_{number}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation/federal",
        title=f"Revenue Procedure {year}-{number}",
        text=text,
        url=url,
        extra_frontmatter={
            "document_type": "revenue_procedure",
            "year": year,
            "number": number,
            "agency": "IRS",
        },
    )
    log.info(f"Fetched Rev. Proc. {year}-{number}: {len(text)} chars")
    return {**result, "url": url, "text_length": len(text)}


# ─── Internal Revenue Code (26 USC) ─────────────────────────────────

def fetch_irc_section(section: str) -> dict:
    """Fetch a section of the Internal Revenue Code (26 USC).

    Reuses fed_register.fetch_usc() with title 26.
    """
    from scrapers.fed_register import fetch_usc
    return fetch_usc(title_num=26, section=section)


def fetch_irc_subtitle(subtitle: str = "A") -> dict:
    """Fetch an IRC subtitle.

    Subtitles:
      A — Income Taxes (§§ 1-1564)
      B — Estate and Gift Taxes (§§ 2001-2801)
      C — Employment Taxes (§§ 3101-3510)
      D — Miscellaneous Excise Taxes (§§ 4001-5000)
      E — Alcohol, Tobacco, Firearms (§§ 5001-5891)
      F — Procedure and Administration (§§ 6001-7874)
    """
    subtitle_ranges = {
        "A": ("1", "1564"),
        "B": ("2001", "2801"),
        "C": ("3101", "3510"),
        "F": ("6001", "7874"),
    }
    if subtitle not in subtitle_ranges:
        return {"error": f"Unknown subtitle: {subtitle}"}

    start, end = subtitle_ranges[subtitle]
    from scrapers.fed_register import fetch_usc
    return fetch_usc(title_num=26, section=start)


# ─── Unified Fetch ───────────────────────────────────────────────────

def fetch(source_type: str = "irm", **kwargs) -> dict:
    """Unified fetch interface matching the stub signature.

    Args:
        source_type: "irm", "treasury_reg", "rev_ruling", "rev_proc", "irc"
        **kwargs: passed to the specific fetch function

    Returns: dict with result or error
    """
    if source_type == "irm":
        return fetch_irm(
            part=kwargs.get("part", 1),
            chapter=kwargs.get("chapter", 1),
            section=kwargs.get("section", 1),
        )
    elif source_type == "treasury_reg":
        return fetch_treasury_reg(
            section=kwargs.get("section"),
            part=kwargs.get("part"),
        )
    elif source_type == "rev_ruling":
        return fetch_rev_ruling(
            year=kwargs.get("year", 2024),
            number=kwargs.get("number", 1),
        )
    elif source_type == "rev_proc":
        return fetch_rev_procedure(
            year=kwargs.get("year", 2024),
            number=kwargs.get("number", 1),
        )
    elif source_type == "irc":
        return fetch_irc_section(section=kwargs.get("section", "61"))
    else:
        return {"error": f"Unknown IRS source type: {source_type}"}
