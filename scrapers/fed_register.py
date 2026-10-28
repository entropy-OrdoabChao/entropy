"""
PHASE_2A_PRIMARY_SOURCE_SCRAPERS
Federal Register / eCFR. Refactored from Hermes law_fetcher.py CFR section.
"""
from __future__ import annotations
import re
import json
import requests
from datetime import date
from typing import Optional

from core.logger import get_logger
from scrapers._base import html_to_text, xml_to_text, save_primary_source

log = get_logger("scraper_fed_reg")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30


def _ecfr_date() -> str:
    return date.today().isoformat()


def fetch_cfr(title_num: int, part: Optional[str] = None, section: Optional[str] = None) -> dict:
    """Fetch CFR from eCFR.gov API (XML) with HTML + cornell fallbacks."""
    log.info(f"Fetching {title_num} CFR" + (f" Part {part}" if part else "") + (f" Sec {section}" if section else ""))

    text = ""
    url = ""

    # Try eCFR XML API first
    if part and section:
        url = f"https://www.ecfr.gov/api/versioner/v1/full/{_ecfr_date()}/title-{title_num}.xml?part={part}&section={section}"
    elif part:
        url = f"https://www.ecfr.gov/api/versioner/v1/full/{_ecfr_date()}/title-{title_num}.xml?part={part}"
    else:
        url = f"https://www.ecfr.gov/api/versioner/v1/structure/{_ecfr_date()}/title-{title_num}.json"

    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/xml"}, timeout=TIMEOUT)
        if resp.status_code == 200:
            ctype = resp.headers.get("Content-Type", "")
            if "xml" in ctype:
                text = xml_to_text(resp.text)
            elif "json" in ctype:
                text = json.dumps(resp.json(), indent=2)
    except Exception as e:
        log.warning(f"eCFR API failed: {e}")

    # Fallback: eCFR HTML
    if not text:
        html_url = f"https://www.ecfr.gov/current/title-{title_num}" + (f"/part-{part}" if part else "")
        try:
            resp = requests.get(html_url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                text = html_to_text(resp.text)
                url = html_url
        except Exception as e:
            log.warning(f"eCFR HTML failed: {e}")

    # Fallback: law.cornell.edu
    if not text:
        cornell_url = f"https://www.law.cornell.edu/cfr/text/{title_num}" + (f"/part-{part}" if part else "")
        try:
            resp = requests.get(cornell_url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                text = html_to_text(resp.text)
                url = cornell_url
        except Exception as e:
            log.warning(f"Cornell CFR failed: {e}")

    if not text:
        return {"error": f"Could not fetch {title_num} CFR Part {part}"}

    source_id = f"cfr_{title_num}" + (f"_part_{part}" if part else "") + (f"_sec_{section}" if section else "")
    title_str = f"{title_num} C.F.R." + (f" Part {part}" if part else "") + (f" Sec {section}" if section else "")

    return save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="regulation",
        title=title_str,
        text=text,
        url=url,
        section=f"Part {part}" if part else "",
    )


def fetch_usc(title_num: int, section: Optional[str] = None) -> dict:
    """Fetch USC from uscode.house.gov with cornell fallback.

    For title-level queries (no section): Cornell first, because
    uscode.house.gov returns a JavaScript navigation page with no content.

    For section-level queries: uscode.house.gov first (has full text),
    Cornell as fallback.
    """
    log.info(f"Fetching {title_num} USC" + (f" Sec {section}" if section else ""))

    text = ""
    url = ""

    if section:
        # Section-level: uscode.house.gov has good content
        url = f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title_num}-section{section}&num=0&edition=prelim"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 500:
                text = html_to_text(resp.text)
                # Quality check: must have actual statutory content, not just nav
                if len(text) < 200 or 'CHANGE NOTICE' in text or 'Search & Browse' in text:
                    text = ""  # nav page, not content
        except Exception as e:
            log.warning(f"uscode.house.gov failed: {e}")

        # Fallback to Cornell for sections
        if not text:
            cornell_url = f"https://www.law.cornell.edu/uscode/text/{title_num}/{section}"
            try:
                resp = requests.get(cornell_url, headers=HEADERS, timeout=TIMEOUT)
                if resp.status_code == 200:
                    text = html_to_text(resp.text)
                    url = cornell_url
            except Exception as e:
                log.warning(f"Cornell USC section failed: {e}")
    else:
        # Title-level: Cornell first (uscode.house.gov returns nav page garbage)
        cornell_url = f"https://www.law.cornell.edu/uscode/text/{title_num}"
        try:
            resp = requests.get(cornell_url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 500:
                text = html_to_text(resp.text)
                url = cornell_url
        except Exception as e:
            log.warning(f"Cornell USC title failed: {e}")

        # Fallback to uscode.house.gov view URL (full content, not browse)
        if not text:
            usc_url = f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title_num}&saved=%7CZ3JhbnVsZWlkOlVTQy1wcmVsaW0tdGl0bGU2&edition=prelim"
            try:
                resp = requests.get(usc_url, headers=HEADERS, timeout=60)
                if resp.status_code == 200 and len(resp.text) > 1000:
                    text = html_to_text(resp.text)
                    url = usc_url
                    # Truncate massive full-title responses
                    if len(text) > 50000:
                        text = text[:50000] + "\n\n[TRUNCATED — full title text exceeds 50KB]"
            except Exception as e:
                log.warning(f"uscode.house.gov view failed: {e}")

    # Quality gate: reject nav pages that slipped through
    if text and ('CHANGE NOTICE' in text[:500] and 'Search & Browse' in text[:500]):
        log.warning(f"Rejecting nav page for {title_num} USC — no real content")
        text = ""

    if not text:
        return {"error": f"Could not fetch {title_num} USC" + (f" Sec {section}" if section else "")}

    source_id = f"usc_{title_num}" + (f"_sec_{section}" if section else "")
    title_str = f"{title_num} U.S.C." + (f" Sec {section}" if section else "")

    return save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="statute",
        title=title_str,
        text=text,
        url=url,
        section=str(section or ""),
    )


def fetch_frcp(rule_num: Optional[str] = None) -> dict:
    """Fetch FRCP from law.cornell.edu."""
    log.info(f"Fetching FRCP" + (f" Rule {rule_num}" if rule_num else ""))
    if rule_num:
        url = f"https://www.law.cornell.edu/rules/frcp/rule_{rule_num}"
    else:
        url = "https://www.law.cornell.edu/rules/frcp"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200:
            text = html_to_text(resp.text)
        else:
            return {"error": f"FRCP fetch failed: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"FRCP fetch failed: {e}"}

    source_id = f"frcp_rule_{rule_num}" if rule_num else "frcp_index"

    return save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="rule",
        title=f"FRCP Rule {rule_num}" if rule_num else "FRCP Index",
        text=text,
        url=url,
        section=f"Rule {rule_num}" if rule_num else "",
    )




def _extract_pdf_bytes(pdf_bytes: bytes, label: str = "") -> str:
    """Extract text from PDF bytes using pdfplumber."""
    import tempfile, os
    try:
        import pdfplumber
    except ImportError:
        log.info(f"  pdfplumber not installed")
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        pages_text = []
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages[:100]:
                try:
                    pt = page.extract_text() or ""
                    if pt.strip():
                        pages_text.append(pt)
                except Exception:
                    pass
            num_pages = len(pdf.pages)
        os.unlink(tmp_path)
        text = "\n\n".join(pages_text)
        log.info(f"  PDF extracted: {label} - {num_pages} pages, {len(text)} chars")
        return text
    except Exception as e:
        log.info(f"  PDF extraction failed for {label}: {e}")
        return ""


def fetch_public_law(congress: int, law_num: int) -> dict:
    """Fetch a Public Law from multiple sources with PDF fallback.

    Sources (in order):
        1. GovInfo HTML (modern laws, ~104th Congress+)
        2. uscode.house.gov HTML (older laws)
        3. GovInfo Statutes at Large PDF (all eras)
        4. GovInfo PLAW PDF (if available)
    """
    log.info(f"Fetching Public Law {congress}-{law_num}")
    text = ""
    url = ""
    title_str = f"Public Law {congress}-{law_num}"

    # Strategy 1: GovInfo HTML (works for ~104th Congress onward)
    govinfo_url = f"https://www.govinfo.gov/content/pkg/PLAW-{congress}publ{law_num}/html/PLAW-{congress}publ{law_num}.htm"
    try:
        resp = requests.get(govinfo_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200 and len(resp.text) > 1000:
            text = html_to_text(resp.text)
            if "Page Not Found" in text or "Error occurred" in text or len(text) < 200:
                text = ""
                log.info(f"  GovInfo HTML returned 404 page")
            else:
                url = govinfo_url
                log.info(f"  Found on GovInfo HTML: {len(text)} chars")
    except Exception as e:
        log.info(f"  GovInfo HTML failed: {e}")

    # Strategy 2: uscode.house.gov HTML (older laws)
    if not text:
        house_url = f"https://uscode.house.gov/statutes/pl/{congress}/{law_num}.htm"
        try:
            resp = requests.get(house_url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 500:
                converted = html_to_text(resp.text)
                if len(converted) > 200 and "Page Not Found" not in converted and "Document not found" not in converted:
                    text = converted
                    url = house_url
                    log.info(f"  Found on uscode.house.gov: {len(text)} chars")
        except Exception as e:
            log.info(f"  uscode.house.gov failed: {e}")

    # Strategy 3: GovInfo Statutes at Large per-page PDFs
    if not text:
        stat_vol = congress - 25  # rough mapping: 73rd Congress ~ 48 Stat
        # Search multiple page ranges — public laws appear at various pages
        pages_to_try = list(range(max(1, law_num - 5), law_num + 50, 5))
        pages_to_try.extend([100, 112, 113, 150, 200, 250])  # common law pages
        tried = set()
        for pg in pages_to_try:
            if pg in tried:
                continue
            tried.add(pg)
            pdf_url = f"https://www.govinfo.gov/content/pkg/STATUTE-{stat_vol}/pdf/STATUTE-{stat_vol}-Pg{pg}.pdf"
            try:
                resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 200 and "pdf" in ct.lower() and len(resp.content) > 10000:
                    extracted = _extract_pdf_bytes(resp.content, f"STATUTE-{stat_vol}-Pg{pg}")
                    if extracted and len(extracted) > 200:
                        # Check if this page contains Public No or the law number
                        has_match = (
                            f"No. {law_num}" in extracted
                            or f"Public, No. {law_num}" in extracted
                            or f"Pub. L. {congress}" in extracted
                            or f"Ch. {law_num}" in extracted.split("\n")[0]
                        )
                        if has_match:
                            text = extracted
                            url = pdf_url
                            title_str = f"Statutes at Large Vol. {stat_vol} Pg {pg} (PL {congress}-{law_num})"
                            log.info(f"  Found matching PDF: STATUTE-{stat_vol}-Pg{pg} ({len(text)} chars)")
                            break
            except Exception:
                pass

    # Strategy 4: GovInfo XML listing to find the exact page
    if not text:
        stat_vol = congress - 25
        # Try the XML package listing to find granule names
        try:
            listing_url = f"https://www.govinfo.gov/content/pkg/STATUTE-{stat_vol}/xml/STATUTE-{stat_vol}.xml"
            resp = requests.get(listing_url, headers=HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.text) > 1000:
                # Search XML for references to this public law number
                import re as _re_xml
                for m in _re_xml.finditer(r"Pg(\d+)", resp.text):
                    pg = int(m.group(1))
                    if pg not in tried:
                        tried.add(pg)
                        # Fetch this page PDF
                        pg_url = f"https://www.govinfo.gov/content/pkg/STATUTE-{stat_vol}/pdf/STATUTE-{stat_vol}-Pg{pg}.pdf"
                        try:
                            pr = requests.get(pg_url, headers=HEADERS, timeout=30)
                            if pr.status_code == 200 and "pdf" in pr.headers.get("content-type","") and len(pr.content) > 5000:
                                ext = _extract_pdf_bytes(pr.content, f"STATUTE-{stat_vol}-Pg{pg}")
                                if ext and f"No. {law_num}" in ext:
                                    text = ext
                                    url = pg_url
                                    title_str = f"Statutes at Large Vol. {stat_vol} Pg {pg} (PL {congress}-{law_num})"
                                    log.info(f"  Found via XML index: STATUTE-{stat_vol}-Pg{pg}")
                                    break
                        except Exception:
                            pass
        except Exception as e:
            log.info(f"  XML index search failed: {e}")

    # Strategy 5: Full volume PDF (last resort — slow but guaranteed)
    if not text:
        stat_vol = congress - 25
        full_pdf_url = f"https://www.govinfo.gov/content/pkg/STATUTE-{stat_vol}/pdf/STATUTE-{stat_vol}.pdf"
        try:
            log.info(f"  Downloading full volume PDF (may be large)...")
            resp = requests.get(full_pdf_url, headers=HEADERS, timeout=120)
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "pdf" in ct.lower() and len(resp.content) > 50000:
                full_text = _extract_pdf_bytes(resp.content, f"STATUTE-{stat_vol}-full")
                if full_text and len(full_text) > 500:
                    # Search for the specific public law within the full text
                    import re as _re_full
                    patterns = [
                        f"Public,? No\.? {law_num}",
                        f"\[Public,? No\.? {law_num}\]",
                        f"Ch\.? {law_num}[^\d]",
                    ]
                    for pat in patterns:
                        m = _re_full.search(pat, full_text)
                        if m:
                            # Extract ~5000 chars around the match
                            start = max(0, m.start() - 500)
                            end = min(len(full_text), m.end() + 5000)
                            text = full_text[start:end]
                            url = full_pdf_url
                            title_str = f"Statutes at Large Vol. {stat_vol} (PL {congress}-{law_num})"
                            log.info(f"  Found in full volume: {len(text)} chars at position {m.start()}")
                            break
                    if not text:
                        # Couldn't find specific law, save whole volume
                        text = full_text
                        url = full_pdf_url
                        title_str = f"Statutes at Large Vol. {stat_vol} (PL {congress}-{law_num})"
                        log.info(f"  Using full volume: {len(text)} chars")
        except Exception as e:
            log.info(f"  Full volume PDF failed: {e}")

    if not text:
        return {"error": f"Could not fetch Public Law {congress}-{law_num} from any source"}

    source_id = f"plaw_{congress}_{law_num}"
    result = save_primary_source(
        source_id=source_id,
        jurisdiction="federal",
        source_type="statute",
        title=title_str,
        text=text,
        url=url,
        extra_frontmatter={"congress": congress, "law_number": law_num},
    )
    result["text_length"] = len(text)
    return result


def fetch_usc_title(title_num: int, year: int = 2023) -> dict:
    """Fetch an entire USC title from GovInfo.
    
    WARNING: Full titles can be 5-20MB of text. Use for bulk ingestion only.
    
    Args:
        title_num: USC title number (1-54)
        year: Edition year (default 2023)
    """
    log.info(f"Fetching USC Title {title_num} ({year} edition)")
    
    url = f"https://www.govinfo.gov/content/pkg/USCODE-{year}-title{title_num}/html/USCODE-{year}-title{title_num}.htm"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=120)  # longer timeout for big files
        if resp.status_code == 200 and "Page Not Found" not in resp.text[:500]:
            text = html_to_text(resp.text)
            log.info(f"  USC Title {title_num}: {len(text)} chars ({len(text)//1024}KB)")
            
            source_id = f"usc_title_{title_num}_{year}"
            result = save_primary_source(
                source_id=source_id,
                jurisdiction="federal",
                source_type="statute",
                title=f"United States Code Title {title_num} ({year})",
                text=text,
                url=url,
                extra_frontmatter={"title_num": title_num, "year": year, "source": "govinfo"},
            )
            result["text_length"] = len(text)
            return result
        else:
            return {"error": f"USC Title {title_num} not found on GovInfo"}
    except Exception as e:
        return {"error": f"Failed to fetch USC Title {title_num}: {e}"}


def fetch_public_law_range(congress: int, start: int = 1, end: int = 500) -> list[dict]:
    """Fetch multiple public laws from a Congress session.
    
    Useful for bulk ingestion. Stops when 5 consecutive 404s are hit.
    """
    results = []
    consecutive_fails = 0
    
    for law_num in range(start, end + 1):
        r = fetch_public_law(congress, law_num)
        if r.get("error"):
            consecutive_fails += 1
            if consecutive_fails >= 5:
                log.info(f"  Stopping at PL {congress}-{law_num} (5 consecutive failures)")
                break
        else:
            consecutive_fails = 0
            results.append(r)
            log.info(f"  PL {congress}-{law_num}: {r.get('text_length', 0)} chars")
    
    log.info(f"  Fetched {len(results)} public laws from Congress {congress}")
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: fed_register.py cfr <title> [part] [section]")
        print("       fed_register.py usc <title> [section]")
        print("       fed_register.py frcp [rule_num]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "cfr":
        t = int(sys.argv[2])
        p = sys.argv[3] if len(sys.argv) > 3 else None
        s = sys.argv[4] if len(sys.argv) > 4 else None
        print(json.dumps(fetch_cfr(t, p, s), indent=2))
    elif cmd == "usc":
        t = int(sys.argv[2])
        s = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(fetch_usc(t, s), indent=2))
    elif cmd == "frcp":
        r = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(fetch_frcp(r), indent=2))
    elif cmd == "plaw":
        congress = int(sys.argv[2])
        law = int(sys.argv[3])
        print(json.dumps(fetch_public_law(congress, law), indent=2, default=str))
    elif cmd == "usc_title":
        t = int(sys.argv[2])
        yr = int(sys.argv[3]) if len(sys.argv) > 3 else 2023
        print(json.dumps(fetch_usc_title(t, yr), indent=2, default=str))
    elif cmd == "plaw_range":
        congress = int(sys.argv[2])
        s_num = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        e_num = int(sys.argv[4]) if len(sys.argv) > 4 else 500
        results = fetch_public_law_range(congress, s_num, e_num)
        print(f"Fetched {len(results)} public laws")