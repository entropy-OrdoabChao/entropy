"""
pacer_download.py - Agnostic PACER document downloader
Handles: SSO login, CSRF pages, receipt pages, multipart POST,
iframe extraction, multi-document entries, permission denied, download ledger.
Designed to be called by Hermes cron or manually.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, requests, re, os, sys
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs

ECF_BASE = "https://ecf.paed.uscourts.gov"

# Path-flexible: container mounts /opt/data; in WSL the repo is <home>.
# Detect at import so the SAME script runs in both without changing the launch.
_DATA = "/opt/data" if os.path.isdir("/opt/data") else str(_eroot())
def _first_existing(*paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[-1]
CREDS_PATH = _first_existing(f"{_DATA}/pacer_credentials.json", f"{_DATA}/pacer_creds.json")
LEDGER_PATH = f"{_DATA}/obsidian_sync/pacer_download_ledger.json"
CASES_BASE = f"{_DATA}/obsidian_sync/cases"

class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.cf = [], None
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cf = {"id": a.get("id",""), "action": a.get("action",""), "inputs": []}
            self.forms.append(self.cf)
        elif tag == "input" and self.cf:
            self.cf["inputs"].append(a)

def load_ledger():
    try:
        return json.load(open(LEDGER_PATH))
    except:
        return {"downloaded": {}, "errors": {}}

def save_ledger(ledger):
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)

def pacer_login():
    creds = json.load(open(CREDS_PATH))
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    r1 = s.get("https://pacer.login.uscourts.gov/csologin/login.jsf")
    p = FormParser()
    p.feed(r1.text)
    vs = ""
    for form in p.forms:
        if form["id"] == "loginForm":
            for inp in form["inputs"]:
                if inp.get("name") == "jakarta.faces.ViewState":
                    vs = inp.get("value", "")
    s.post("https://pacer.login.uscourts.gov/csologin/login.jsf", data={
        "loginForm": "loginForm", "loginForm:loginName": creds["username"],
        "loginForm:password": creds["password"], "loginForm:clientCode": creds["client_code"],
        "loginForm:courtId_input": "", "loginForm:courtId_filter": "",
        "jakarta.faces.ViewState": vs, "loginForm:fbtnLogin": "",
    }, allow_redirects=True)
    return s

def _save_pdf(content, save_path, ledger, ledger_key):
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(content)
    today = datetime.now().strftime("%Y-%m-%d")
    ledger.setdefault("downloaded", {})[ledger_key] = {
        "date": today, "path": save_path, "size": len(content)
    }
    print(f"  SAVED: {os.path.basename(save_path)} ({len(content)} bytes)")
    return True

def _download_single_doc(s, doc_url, save_path, ledger, ledger_key):
    """Download a single PACER document through receipt + iframe flow."""
    ref = f"{ECF_BASE}/cgi-bin/DktRpt.pl"
    r = s.get(doc_url, headers={"Referer": ref})

    # SAFE DIAGNOSE: capture the first-GET response and STOP before the receipt step (no free look spent).
    import os as _os
    if _os.environ.get("PACER_DIAGNOSE"):
        _dbg = str(_eroot() / "output/_pacer_diag.html")
        try:
            with open(_dbg, "w", encoding="utf-8") as _f:
                _f.write(r.text)
        except Exception:
            pass
        _pdf = "yes" if r.content[:4] == b"%PDF" else "no"
        print(f"  DIAGNOSE: {len(r.text)} bytes | pdf={_pdf} | ctype={r.headers.get('content-type')} | url={r.url}")
        print(f"  DIAGNOSE: body saved -> {_dbg}")
        return False

    # Case 1: Direct PDF
    if r.content[:4] == b"%PDF":
        return _save_pdf(r.content, save_path, ledger, ledger_key)

    # Case 2: Permission denied
    if "do not have permission" in r.text.lower():
        print(f"  PERMISSION DENIED - restricted document")
        ledger.setdefault("errors", {})[ledger_key] = {
            "date": datetime.now().strftime("%Y-%m-%d"), "error": "permission_denied"
        }
        return False

    # Case 3: Receipt page (has "View Document" or "Transaction Receipt")
    if "View Document" in r.text or "Transaction Receipt" in r.text:
        cost = re.search(r'Cost:.*?(\d+\.\d+)', r.text)
        if cost:
            print(f"  Cost: ${cost.group(1)}")
        # Extract form action
        action_match = re.search(r"action='([^']+)'", r.text)
        action_url = action_match.group(1) if action_match else doc_url.split("?")[0]
        if not action_url.startswith("http"):
            action_url = ECF_BASE + action_url
        # Extract goDLS params
        godls = re.search(r"goDLS\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", r.text)
        params = {}
        if godls:
            params = {"caseid": godls.group(2), "de_seq_num": godls.group(3), "got_receipt": godls.group(4)}
            if godls.group(6):
                params["pdf_toggle_possible"] = godls.group(6)
        else:
            qs = parse_qs(urlparse(doc_url).query)
            params = {"caseid": qs.get("caseid",[""])[0], "de_seq_num": qs.get("de_seq_num",[""])[0], "got_receipt": "1"}
        # POST multipart to get iframe page
        r2 = s.post(action_url, files={k: (None, v) for k, v in params.items()}, headers={"Referer": doc_url})
        # Extract iframe with temp PDF
        iframe = re.search(r'src="(/cgi-bin/show_temp\.pl\?file=[^"]+)"', r2.text)
        if iframe:
            pdf_url = ECF_BASE + iframe.group(1).replace("&amp;", "&")
            r3 = s.get(pdf_url, headers={"Referer": r2.url})
            if r3.content[:4] == b"%PDF":
                return _save_pdf(r3.content, save_path, ledger, ledger_key)
        if r2.content[:4] == b"%PDF":
            return _save_pdf(r2.content, save_path, ledger, ledger_key)
        print(f"  Failed to extract PDF from receipt flow")
        return False

    # Case 4: Unknown page
    print(f"  Unknown response ({len(r.text)} bytes)")
    return False

def download_pacer_doc(s, doc_url, save_dir, case_num, dkt_num, desc, ledger):
    """
    Agnostic download - handles single docs, multi-doc entries, permission denied.
    Returns list of downloaded file paths.
    """
    base_key = f"{case_num}_dkt_{dkt_num}"
    downloaded = []

    # Check if already in ledger (any variant)
    existing = [k for k in ledger.get("downloaded", {}) if k.startswith(base_key)]
    if existing:
        print(f"  SKIP - already in ledger ({len(existing)} files)")
        return []

    # Check if permission was previously denied
    if base_key in ledger.get("errors", {}):
        err = ledger["errors"][base_key]
        if err.get("error") == "permission_denied":
            print(f"  SKIP - previously denied permission")
            return []

    ref = f"{ECF_BASE}/cgi-bin/DktRpt.pl"
    r = s.get(doc_url, headers={"Referer": ref})

    # Detect multi-document listing page
    doc_links = re.findall(r'href="(https://ecf\.[^"]*doc1/\d+)"', r.text)
    is_multi = len(doc_links) > 1 or "file_too_big" in r.text or "view each document individually" in r.text.lower()

    if is_multi:
        print(f"  Multi-document entry: {len(doc_links)} parts")
        # Extract caseid and de_seq_num from original URL
        qs = parse_qs(urlparse(doc_url).query)
        caseid = qs.get("caseid", [""])[0]
        de_seq = qs.get("de_seq_num", [""])[0]

        for i, link in enumerate(doc_links):
            part_key = f"{base_key}_part{i}" if i > 0 else base_key
            part_desc = f"{desc}_part{i}" if i > 0 else desc
            save_path = f"{save_dir}/dkt_{dkt_num}_{part_desc}.pdf"

            if part_key in ledger.get("downloaded", {}):
                print(f"  SKIP part {i}")
                continue
            if os.path.exists(save_path):
                with open(save_path, "rb") as f:
                    if f.read(4) == b"%PDF":
                        print(f"  SKIP part {i} - PDF exists")
                        ledger.setdefault("downloaded", {})[part_key] = {"date": datetime.now().strftime("%Y-%m-%d"), "path": save_path}
                        continue

            # Each sub-doc needs caseid and de_seq_num
            sub_url = f"{link}?caseid={caseid}&de_seq_num={de_seq}"
            print(f"  Part {i}: {os.path.basename(link)}")
            if _download_single_doc(s, sub_url, save_path, ledger, part_key):
                downloaded.append(save_path)
        return downloaded

    # Single document or receipt page
    save_path = f"{save_dir}/dkt_{dkt_num}_{desc}.pdf"
    if os.path.exists(save_path):
        with open(save_path, "rb") as f:
            if f.read(4) == b"%PDF":
                print(f"  SKIP - PDF exists")
                ledger.setdefault("downloaded", {})[base_key] = {"date": datetime.now().strftime("%Y-%m-%d"), "path": save_path}
                return []
            else:
                os.remove(save_path)

    # Permission denied
    if "do not have permission" in r.text.lower():
        print(f"  PERMISSION DENIED")
        ledger.setdefault("errors", {})[base_key] = {"date": datetime.now().strftime("%Y-%m-%d"), "error": "permission_denied"}
        return []

    # Receipt page - use existing response
    if "View Document" in r.text or "Transaction Receipt" in r.text:
        cost = re.search(r'Cost:.*?(\d+\.\d+)', r.text)
        if cost:
            print(f"  Cost: ${cost.group(1)}")
        action_match = re.search(r"action='([^']+)'", r.text)
        action_url = action_match.group(1) if action_match else doc_url.split("?")[0]
        if not action_url.startswith("http"):
            action_url = ECF_BASE + action_url
        godls = re.search(r"goDLS\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", r.text)
        params = {}
        if godls:
            params = {"caseid": godls.group(2), "de_seq_num": godls.group(3), "got_receipt": godls.group(4)}
            if godls.group(6):
                params["pdf_toggle_possible"] = godls.group(6)
        else:
            qs = parse_qs(urlparse(doc_url).query)
            params = {"caseid": qs.get("caseid",[""])[0], "de_seq_num": qs.get("de_seq_num",[""])[0], "got_receipt": "1"}
        r2 = s.post(action_url, files={k: (None, v) for k, v in params.items()}, headers={"Referer": doc_url})
        iframe = re.search(r'src="(/cgi-bin/show_temp\.pl\?file=[^"]+)"', r2.text)
        if iframe:
            pdf_url = ECF_BASE + iframe.group(1).replace("&amp;", "&")
            r3 = s.get(pdf_url, headers={"Referer": r2.url})
            if r3.content[:4] == b"%PDF":
                if _save_pdf(r3.content, save_path, ledger, base_key):
                    downloaded.append(save_path)
                    return downloaded
        if r2.content[:4] == b"%PDF":
            if _save_pdf(r2.content, save_path, ledger, base_key):
                downloaded.append(save_path)
                return downloaded
        print(f"  Failed receipt flow")
        return []

    # Direct PDF from initial GET
    if r.content[:4] == b"%PDF":
        if _save_pdf(r.content, save_path, ledger, base_key):
            downloaded.append(save_path)
            return downloaded

    print(f"  Unknown response ({len(r.text)} bytes)")
    return []


if __name__ == "__main__":
    # Can be called with args: pacer_download.py <case_num> <dkt_num> <slug> <desc> <url>
    # Or with no args to process from a JSON task file
    ledger = load_ledger()
    s = pacer_login()
    print("LOGIN SUCCESS")

    if len(sys.argv) > 5:
        case_num, dkt_num, slug, desc, url = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
        save_dir = f"{CASES_BASE}/{slug}/intake"
        os.makedirs(save_dir, exist_ok=True)
        result = download_pacer_doc(s, url, save_dir, case_num, dkt_num, desc, ledger)
        save_ledger(ledger)
        print(f"Downloaded: {len(result)} files")
    else:
        # Process from task file if exists
        task_path = "/opt/data/pacer_download_tasks.json"
        if os.path.exists(task_path):
            tasks = json.load(open(task_path))
            dl, sk = 0, 0
            for t in tasks:
                save_dir = f"{CASES_BASE}/{t['slug']}/intake"
                os.makedirs(save_dir, exist_ok=True)
                print(f"\n[{t['case']} Dkt {t['dkt']}] {t['desc']}")
                result = download_pacer_doc(s, t["url"], save_dir, t["case"], t["dkt"], t["desc"], ledger)
                dl += len(result)
                if not result:
                    sk += 1
            save_ledger(ledger)
            print(f"\n=== DONE: {dl} downloaded, {sk} skipped, {len(ledger.get('downloaded',{}))} in ledger ===")
        else:
            print("No task file and no args. Usage: pacer_download.py <case> <dkt> <slug> <desc> <url>")
