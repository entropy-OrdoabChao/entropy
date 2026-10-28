#!/usr/bin/env python3
"""
law_fetcher.py — Fetch statutes/regulations from official .gov sources.
Priority: HTML → XML → TXT → PDF (fastest to slowest, least throttled)

Sources:
  PA statutes:  palegis.us (HTML)
  USC:          uscode.house.gov (XML/HTML)
  CFR/eCFR:     ecfr.gov (XML/JSON API)
  FRCP/FRE:     uscode.house.gov or law.cornell.edu

Usage:
  law_fetcher.py --pa-title 40                    # PA Title 40 (Insurance)
  law_fetcher.py --pa-title 42 --section 8371     # Specific PA section
  law_fetcher.py --usc-title 28 --section 1331    # 28 USC 1331
  law_fetcher.py --cfr-title 12 --part 1026       # 12 CFR 1026 (Reg Z)
  law_fetcher.py --ingest                         # Auto-ingest fetched text into ChromaDB
"""

import argparse, json, re, sys, os
from pathlib import Path
from html.parser import HTMLParser

# Try requests
try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
OUTPUT_DIR = Path("/tmp/law_fetcher")


class HTMLTextExtractor(HTMLParser):
    """Extract text from HTML, skipping scripts/styles."""
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav', 'header', 'footer', 'noscript'):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'header', 'footer', 'noscript'):
            self.skip = False
        if tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'h4', 'li', 'tr', 'dt', 'dd', 'td'):
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def extract_text_from_html(html):
    """Clean HTML to plain text."""
    parser = HTMLTextExtractor()
    parser.feed(html)
    text = "".join(parser.text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def fetch_url(url, accept="text/html"):
    """Fetch URL with proper headers. Returns (text, content_type) or (None, None)."""
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": accept}, timeout=30)
        if resp.status_code == 200:
            return resp.text, resp.headers.get("Content-Type", "")
        else:
            print(f"  HTTP {resp.status_code} from {url[:80]}")
            return None, None
    except Exception as e:
        print(f"  Error fetching {url[:80]}: {e}")
        return None, None


# ═══════════════════════════════════════════════════════════════════
# PA STATUTES — palegis.us
# ═══════════════════════════════════════════════════════════════════

def fetch_with_browser(url, wait_selector=None, wait_seconds=3):
    """Fetch a JS-rendered page using Playwright headless browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed. Run: python3 -m pip install playwright && python3 -m playwright install chromium")
        return None

    print(f"  Using headless browser for: {url[:80]}...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait for content to render
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except:
                    pass

            import time
            time.sleep(wait_seconds)

            # Get the full rendered text
            text = page.inner_text("body")
            browser.close()
            return text
    except Exception as e:
        print(f"  Browser fetch failed: {e}")
        return None


def fetch_pa_title(title_num, section=None):
    """
    Fetch PA consolidated statutes from palegis.us.
    The site loads content via iframe — use the iframe URL directly.
    """
    print(f"Fetching PA Title {title_num}...")

    # The iframe URL is the one that returns actual statute content
    url = f"https://www.palegis.us/statutes/consolidated/view-statute?34&iFrame=true&txtType=HTM&ttl={title_num}"
    html, ctype = fetch_url(url)

    if not html or len(html) < 5000:
        # Try unconsolidated
        url = f"https://www.palegis.us/statutes/unconsolidated/view-statute?34&iFrame=true&txtType=HTM&ttl={title_num}"
        html, ctype = fetch_url(url)

    if not html or len(html) < 5000:
        print(f"  FAILED: Could not fetch PA Title {title_num}")
        return None

    text = extract_text_from_html(html)
    # Fix HTML entity for section symbol
    text = text.replace("\u00a7", "§")
    print(f"  Fetched: {len(text):,} chars")

    # If specific section requested, try to extract it
    if section:
        section_text = _extract_pa_section(text, section)
        if section_text:
            text = section_text
            print(f"  Extracted section {section}: {len(text):,} chars")
        else:
            print(f"  Warning: Could not isolate section {section}, returning full title")

    result = {
        "source": f"pa_title_{title_num}",
        "title": f"PA Title {title_num}",
        "section": str(section) if section else "",
        "url": url,
        "text": text,
        "jurisdiction": "PA",
        "type": "statute",
    }

    _save_result(result)
    return result


def _extract_pa_section(text, section):
    """Try to extract a specific section from the full title text."""
    section_str = str(section)
    # Try patterns like "§ 8371" or "Section 8371"
    patterns = [
        rf'§\s*{section_str}\.?\s*(.*?)(?=§\s*\d|$)',
        rf'Section\s+{section_str}\.?\s*(.*?)(?=Section\s+\d|$)',
        rf'{section_str}\.\s*(.*?)(?=\d{{4}}\.|$)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m and len(m.group(0)) > 100:
            return m.group(0)[:10000]
    return None


# ═══════════════════════════════════════════════════════════════════
# USC — uscode.house.gov
# ═══════════════════════════════════════════════════════════════════

def fetch_usc(title_num, section=None):
    """
    Fetch USC from uscode.house.gov.
    HTML format available.
    """
    print(f"Fetching {title_num} USC{f' § {section}' if section else ''}...")

    if section:
        # Specific section
        url = f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title_num}-section{section}&num=0&edition=prelim"
    else:
        # Full title TOC
        url = f"https://uscode.house.gov/browse/prelim@title{title_num}&edition=prelim"

    html, ctype = fetch_url(url)

    if not html or len(html) < 500:
        # Fallback: law.cornell.edu
        print(f"  Trying law.cornell.edu...")
        if section:
            url = f"https://www.law.cornell.edu/uscode/text/{title_num}/{section}"
        else:
            url = f"https://www.law.cornell.edu/uscode/text/{title_num}"
        html, ctype = fetch_url(url)

    if not html:
        print(f"  FAILED: Could not fetch {title_num} USC § {section}")
        return None

    text = extract_text_from_html(html)
    print(f"  Fetched: {len(text):,} chars")

    result = {
        "source": f"usc_{title_num}_{section}" if section else f"usc_{title_num}",
        "title": f"{title_num} U.S.C.",
        "section": str(section) if section else "",
        "url": url,
        "text": text,
        "jurisdiction": "federal",
        "type": "statute",
    }

    _save_result(result)
    return result


# ═══════════════════════════════════════════════════════════════════
# CFR — ecfr.gov (API available)
# ═══════════════════════════════════════════════════════════════════

def fetch_cfr(title_num, part=None, section=None):
    """
    Fetch CFR from eCFR.gov.
    Has a JSON API — fastest, no HTML parsing needed.
    """
    print(f"Fetching {title_num} CFR{f' Part {part}' if part else ''}{f' § {section}' if section else ''}...")

    # eCFR API (XML format)
    if part and section:
        url = f"https://www.ecfr.gov/api/versioner/v1/full/{get_ecfr_date()}/title-{title_num}.xml?part={part}&section={section}"
    elif part:
        url = f"https://www.ecfr.gov/api/versioner/v1/full/{get_ecfr_date()}/title-{title_num}.xml?part={part}"
    else:
        url = f"https://www.ecfr.gov/api/versioner/v1/structure/{get_ecfr_date()}/title-{title_num}.json"

    content, ctype = fetch_url(url, accept="application/xml")

    if content and "xml" in (ctype or ""):
        # Parse XML to text
        text = _xml_to_text(content)
    elif content and "json" in (ctype or ""):
        text = json.dumps(json.loads(content), indent=2)
    else:
        # Fallback: HTML version
        print("  Trying HTML fallback...")
        if part:
            url = f"https://www.ecfr.gov/current/title-{title_num}/part-{part}"
        else:
            url = f"https://www.ecfr.gov/current/title-{title_num}"
        html, _ = fetch_url(url)
        text = extract_text_from_html(html) if html else ""

    if not text:
        # Final fallback: law.cornell.edu
        print("  Trying law.cornell.edu...")
        if part:
            url = f"https://www.law.cornell.edu/cfr/text/{title_num}/part-{part}"
        else:
            url = f"https://www.law.cornell.edu/cfr/text/{title_num}"
        html, _ = fetch_url(url)
        text = extract_text_from_html(html) if html else ""

    if not text:
        print(f"  FAILED: Could not fetch {title_num} CFR Part {part}")
        return None

    print(f"  Fetched: {len(text):,} chars")

    result = {
        "source": f"cfr_{title_num}_{part}" if part else f"cfr_{title_num}",
        "title": f"{title_num} C.F.R.",
        "section": f"Part {part}" if part else "",
        "url": url,
        "text": text,
        "jurisdiction": "federal",
        "type": "regulation",
    }

    _save_result(result)
    return result


def get_ecfr_date():
    """Get current date for eCFR API."""
    from datetime import date
    return date.today().isoformat()


def _xml_to_text(xml_content):
    """Simple XML to text extraction."""
    # Strip XML tags
    text = re.sub(r'<[^>]+>', ' ', xml_content)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# FRCP / FRE — from law.cornell.edu
# ═══════════════════════════════════════════════════════════════════

def fetch_frcp(rule_num=None):
    """Fetch Federal Rules of Civil Procedure."""
    print(f"Fetching FRCP{f' Rule {rule_num}' if rule_num else ''}...")

    if rule_num:
        url = f"https://www.law.cornell.edu/rules/frcp/rule_{rule_num}"
    else:
        url = "https://www.law.cornell.edu/rules/frcp"

    html, _ = fetch_url(url)
    if not html:
        print(f"  FAILED: Could not fetch FRCP Rule {rule_num}")
        return None

    text = extract_text_from_html(html)
    print(f"  Fetched: {len(text):,} chars")

    result = {
        "source": f"frcp_rule_{rule_num}" if rule_num else "frcp",
        "title": "FRCP",
        "section": f"Rule {rule_num}" if rule_num else "",
        "url": url,
        "text": text,
        "jurisdiction": "federal",
        "type": "rule",
    }

    _save_result(result)
    return result


def fetch_fre(rule_num=None):
    """Fetch Federal Rules of Evidence."""
    print(f"Fetching FRE{f' Rule {rule_num}' if rule_num else ''}...")

    if rule_num:
        url = f"https://www.law.cornell.edu/rules/fre/rule_{rule_num}"
    else:
        url = "https://www.law.cornell.edu/rules/fre"

    html, _ = fetch_url(url)
    if not html:
        print(f"  FAILED: Could not fetch FRE Rule {rule_num}")
        return None

    text = extract_text_from_html(html)
    print(f"  Fetched: {len(text):,} chars")

    result = {
        "source": f"fre_rule_{rule_num}" if rule_num else "fre",
        "title": "FRE",
        "section": f"Rule {rule_num}" if rule_num else "",
        "url": url,
        "text": text,
        "jurisdiction": "federal",
        "type": "rule",
    }

    _save_result(result)
    return result


# ═══════════════════════════════════════════════════════════════════
# SAVE & INGEST
# ═══════════════════════════════════════════════════════════════════

def _save_result(result):
    """Save fetched law to temp file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outfile = OUTPUT_DIR / f"{result['source']}.txt"
    outfile.write_text(result["text"], encoding="utf-8")
    print(f"  Saved: {outfile}")

    # Also save metadata
    meta_file = OUTPUT_DIR / f"{result['source']}.json"
    meta = {k: v for k, v in result.items() if k != "text"}
    meta["text_length"] = len(result["text"])
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2)


def ingest_fetched(chroma_host="localhost", chroma_port=8100):
    """Ingest all fetched law files into ChromaDB."""
    if not OUTPUT_DIR.exists():
        print("No fetched files to ingest")
        return

    txt_files = list(OUTPUT_DIR.glob("*.txt"))
    if not txt_files:
        print("No .txt files to ingest")
        return

    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("ERROR: pip install chromadb sentence-transformers")
        return

    client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
    col = client.get_collection("legal_docs")
    model = SentenceTransformer("BAAI/bge-base-en-v1.5")
    print(f"Connected: {col.count():,} chunks")

    import hashlib
    total = 0

    for txt_file in txt_files:
        meta_file = txt_file.with_suffix(".json")
        meta = {}
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())

        text = txt_file.read_text(encoding="utf-8")
        if len(text.strip()) < 100:
            continue

        source = meta.get("source", txt_file.stem)
        law_type = meta.get("type", "text")
        jurisdiction = meta.get("jurisdiction", "")

        # Chunk at 400 words, 50 overlap
        words = text.split()
        chunks = []
        i = 0
        idx = 0
        while i < len(words):
            chunk = " ".join(words[i:i + 400])
            if len(chunk.strip()) >= 50:
                chunk_id = hashlib.md5(f"{source}_{idx}".encode()).hexdigest()
                chunks.append({
                    "id": chunk_id,
                    "text": chunk,
                    "meta": {
                        "source": source,
                        "chunk_type": law_type,
                        "chunk_index": idx,
                        "section": meta.get("section", ""),
                        "citations": "",
                        "term": "",
                        "source_name": meta.get("title", ""),
                        "source_category": jurisdiction,
                    }
                })
                idx += 1
            i += 350

        if not chunks:
            continue

        # Batch ingest
        batch_size = 100
        for b in range(0, len(chunks), batch_size):
            batch = chunks[b:b + batch_size]
            texts = [c["text"] for c in batch]
            ids = [c["id"] for c in batch]
            metas = [c["meta"] for c in batch]
            vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()
            col.upsert(ids=ids, documents=texts, embeddings=vecs, metadatas=metas)

        total += len(chunks)
        print(f"  Ingested {len(chunks)} chunks from {txt_file.name}")

    print(f"\nTotal ingested: {total}")
    print(f"Collection: {col.count():,} chunks")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Fetch laws from official .gov sources")
    parser.add_argument("--pa-title", type=int, help="PA consolidated statute title number")
    parser.add_argument("--usc-title", type=int, help="USC title number")
    parser.add_argument("--cfr-title", type=int, help="CFR title number")
    parser.add_argument("--part", type=str, help="CFR part number")
    parser.add_argument("--section", type=str, help="Specific section number")
    parser.add_argument("--frcp", type=str, nargs="?", const="all", help="FRCP rule number (or 'all')")
    parser.add_argument("--fre", type=str, nargs="?", const="all", help="FRE rule number (or 'all')")
    parser.add_argument("--ingest", action="store_true", help="Ingest fetched files into ChromaDB")
    parser.add_argument("--chroma-host", default="localhost")
    parser.add_argument("--chroma-port", type=int, default=8100)

    args = parser.parse_args()

    if args.pa_title:
        fetch_pa_title(args.pa_title, args.section)
    elif args.usc_title:
        fetch_usc(args.usc_title, args.section)
    elif args.cfr_title:
        fetch_cfr(args.cfr_title, args.part, args.section)
    elif args.frcp:
        rule = None if args.frcp == "all" else args.frcp
        fetch_frcp(rule)
    elif args.fre:
        rule = None if args.fre == "all" else args.fre
        fetch_fre(rule)
    elif args.ingest:
        ingest_fetched(args.chroma_host, args.chroma_port)
    else:
        parser.print_help()

    if args.ingest and not (args.pa_title or args.usc_title or args.cfr_title or args.frcp or args.fre):
        pass  # already handled
    elif args.ingest:
        ingest_fetched(args.chroma_host, args.chroma_port)


if __name__ == "__main__":
    main()
