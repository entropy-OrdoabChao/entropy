"""
PHASE_2A_PRIMARY_SOURCE_SCRAPERS
PA consolidated statutes from palegis.us. Refactored from Hermes law_fetcher.py.
"""
from __future__ import annotations
import re
import requests
from typing import Optional

from core.logger import get_logger
from scrapers._base import html_to_text, save_primary_source

log = get_logger("scraper_pa_legis")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30


def fetch_title(title_num: int, section: Optional[str] = None) -> dict:
    """Fetch PA consolidated statute title. Optionally extract a specific section."""
    log.info(f"Fetching PA Title {title_num}" + (f" section {section}" if section else ""))

    url = f"https://www.palegis.us/statutes/consolidated/view-statute?34&iFrame=true&txtType=HTM&ttl={title_num}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        html = resp.text if resp.status_code == 200 else ""
    except Exception as e:
        log.error(f"PA legis fetch failed: {e}")
        html = ""

    if not html or len(html) < 5000:
        # Try unconsolidated
        url = f"https://www.palegis.us/statutes/unconsolidated/view-statute?34&iFrame=true&txtType=HTM&ttl={title_num}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            html = resp.text if resp.status_code == 200 else ""
        except Exception as e:
            log.error(f"PA legis unconsolidated fetch failed: {e}")
            return {"error": f"Could not fetch PA Title {title_num}"}

    if not html or len(html) < 5000:
        return {"error": f"Could not fetch PA Title {title_num}"}

    text = html_to_text(html)

    if section:
        extracted = _extract_section(text, section)
        if extracted:
            text = extracted

    source_id = f"pa_title_{title_num}" + (f"_sec_{section}" if section else "")
    title_str = f"PA Title {title_num}" + (f" Section {section}" if section else "")

    return save_primary_source(
        source_id=source_id,
        jurisdiction="PA",
        source_type="statute",
        title=title_str,
        text=text,
        url=url,
        section=str(section or ""),
    )


def _extract_section(text: str, section: str) -> Optional[str]:
    """Try to extract a specific section from full title text."""
    for pattern in [
        rf"(?:\u00a7|Sec(?:tion)?\.?)\s*{re.escape(section)}\.?\s*(.*?)(?=(?:\u00a7|Sec(?:tion)?\.?)\s*\d|$)",
    ]:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m and len(m.group(0)) > 100:
            return m.group(0)[:10000]
    return None


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: pa_legis.py <title_num> [section]")
        sys.exit(1)
    t = int(sys.argv[1])
    s = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(fetch_title(t, s), indent=2))