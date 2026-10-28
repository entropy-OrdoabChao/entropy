"""
PHASE_2A_PRIMARY_SOURCE_SCRAPERS
PA Code (administrative regulations) from pacodeandbulletin.gov.
"""
from __future__ import annotations
import re
import requests
from typing import Optional

from core.logger import get_logger
from scrapers._base import html_to_text, save_primary_source

log = get_logger("scraper_pa_code")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30


def fetch_title(title_num: int, chapter: Optional[str] = None) -> dict:
    """Fetch PA administrative code title."""
    log.info(f"Fetching PA Code Title {title_num}" + (f" Chapter {chapter}" if chapter else ""))

    if chapter:
        url = f"https://www.pacodeandbulletin.gov/Display/pacode?file=/secure/pacode/data/0{title_num:02d}/chapter{chapter}/chap{chapter}toc.html&d="
    else:
        url = f"https://www.pacodeandbulletin.gov/Display/pacode?file=/secure/pacode/data/0{title_num:02d}/0{title_num:02d}toc.html&d="

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        html = resp.text if resp.status_code == 200 else ""
    except Exception as e:
        log.error(f"PA Code fetch failed: {e}")
        return {"error": f"Could not fetch PA Code Title {title_num}"}

    if not html or len(html) < 500:
        return {"error": f"Could not fetch PA Code Title {title_num}", "url": url}

    text = html_to_text(html)

    source_id = f"pa_code_title_{title_num}" + (f"_ch_{chapter}" if chapter else "")
    title_str = f"PA Code Title {title_num}" + (f" Chapter {chapter}" if chapter else "")

    return save_primary_source(
        source_id=source_id,
        jurisdiction="PA",
        source_type="regulation",
        title=title_str,
        text=text,
        url=url,
        section=str(chapter or ""),
    )


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: pa_code.py <title_num> [chapter]")
        sys.exit(1)
    t = int(sys.argv[1])
    ch = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(fetch_title(t, ch), indent=2))