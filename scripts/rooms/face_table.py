"""FACE TABLE — deterministic face-of-filing scan of an adverse filing (the guardrail; agnostic to case/court).
Rows are FACTS about the document's face with file:line evidence; the procedural/proof lanes decide what each
means under the forum's rules. Checks: caption typos, exhibits referenced vs attached, docket/control numbers,
dates and day-gaps vs the record, signature block / counsel IDs, how the filing designates the opposing party,
verification/oath, AI/non-responsive accusations (prima facie material).
usage: face_table.py <adverse.md> <verified_record.md> <out.md>"""
import re, sys, json, difflib
from datetime import datetime

CAPTION_VOCAB = ["PLAINTIFF", "PLAINTIFFS", "DEFENDANT", "DEFENDANTS", "PETITIONER", "RESPONDENT", "APPELLANT", "APPELLEE",
                 "COURT", "COMMON", "PLEAS", "COUNTY", "PENNSYLVANIA", "UNITED", "STATES", "DISTRICT", "EASTERN", "CIVIL",
                 "DIVISION", "ACTION", "REPLY", "BRIEF", "RESPONSE", "OPPOSITION", "PRELIMINARY", "OBJECTIONS", "MOTION",
                 "MEMORANDUM", "SUPPORT", "ORDER", "COMPANY", "INSURANCE", "CORPORATION", "ENTITIES", "VERSUS", "TERM",
                 "SESSION", "COMMONWEALTH", "SUPERIOR", "SUPREME", "APPEALS", "CIRCUIT", "AMENDED", "COMPLAINT", "ANSWER",
                 "NEW", "MATTER", "COUNTERCLAIM", "NOTICE", "CERTIFICATE", "SERVICE", "VERIFICATION", "AFFIDAVIT"]
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"

def dates_in(txt):
    out = []
    for m in re.finditer(rf"\b({MONTHS})\.? (\d{{1,2}}),? (\d{{4}})\b", txt):
        try: out.append((datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y"), m.group(0), m.start()))
        except ValueError: pass
    for m in re.finditer(r"\b(\d{1,2}) (SEP|OCT|NOV|DEC|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG)[A-Z]* (\d{4})\b", txt):
        out.append((datetime.strptime(f"{m.group(2)[:3].title()} {m.group(1)} {m.group(3)}", "%b %d %Y"), m.group(0), m.start()))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", txt):
        try: out.append((datetime.strptime(m.group(0), "%m/%d/%Y"), m.group(0), m.start()))
        except ValueError: pass
    return out

def lineno(txt, pos): return txt.count("\n", 0, pos) + 1

def build(adv_path, rec_path, out):
    A = open(adv_path, errors="ignore").read(); R = open(rec_path, errors="ignore").read() if rec_path and rec_path != "-" else ""
    aname = adv_path.split("/")[-1]; rname = rec_path.split("/")[-1] if R else "record"
    rows = []  # (check, finding, evidence, status)
    # 1. caption typos: ALL-CAPS words in the first 25 lines within edit distance 1-2 of caption vocabulary
    head = "\n".join(A.splitlines()[:25])
    for m in re.finditer(r"\b[A-Z]{5,}\b", head):
        w = m.group(0)
        if w in CAPTION_VOCAB: continue
        close = difflib.get_close_matches(w, CAPTION_VOCAB, n=1, cutoff=0.8)
        if close and close[0] != w and abs(len(close[0]) - len(w)) <= 2:
            rows.append(("caption", f'misspelling "{w}" (expected "{close[0]}")', f"{aname}:{lineno(A, m.start())}", "DEFECT"))
    # 2. exhibits referenced vs attached
    exs = sorted(set(re.findall(r"\bExhibit[s]? ([A-Z]|\d{1,2})\b", A)))
    att = [(lineno(R, m.start()), R[max(0, m.start()-60):m.end()+60].replace("\n", " ")) for m in re.finditer(r"(?i)\b(no exhibits|zero exhibits|0 exhibits|attached no|without exhibits|exhibits? attached|attached as exhibit)", R)]
    if exs:
        rows.append(("exhibits", f"filing references Exhibit(s) {', '.join(exs)}", "; ".join(f"{aname}:{lineno(A, m.start())}" for m in re.finditer(r"\bExhibit[s]? (?:[A-Z]|\d{1,2})\b", A)), "VERIFY attached?"))
        for ln, ctx in att[:3]:
            rows.append(("exhibits", f"record on attachments: …{ctx.strip()}…", f"{rname}:{ln}", "DEFECT" if re.search(r"(?i)no exhibits|zero exhibits|0 exhibits|attached no", ctx) else "OK"))
    else:
        rows.append(("exhibits", "filing references no exhibits", aname, "OK"))
    # 3. docket / control numbers on the face
    docket = re.findall(r"\b(?:NO|No|Case ID|Docket)\.? ?:? ?(\d{6,}|\d{1,2}:\d{2}-cv-\d{3,5})", A)
    ctrl_a = re.findall(r"Control (?:No\.?|Number)[:\s]*(\d{6,})", A); ctrl_r = re.findall(r"Control (?:No\.?|Number)[:\s]*(\d{6,})", R)
    rows.append(("docket no.", f"case number on face: {', '.join(sorted(set(docket))) or 'NONE'}", f"{aname}:1-25", "OK" if docket else "DEFECT"))
    rows.append(("control no.", f"Control No. on face: {', '.join(sorted(set(ctrl_a))) or 'NONE'}; record's Control No.: {', '.join(sorted(set(ctrl_r))) or 'n/a'}", f"{aname}; {rname}", "OK" if ctrl_a else ("DEFECT" if ctrl_r else "VERIFY forum requires?")))
    # 4. dates: filing/service date of this filing vs the record's prior filing dates → day gaps (the lane applies the rule window)
    da = dates_in(A); dr = dates_in(R)
    cos = [d for d in da if re.search(r"(?i)certif", A[max(0, d[2]-400):d[2]])]
    this_filed = max(cos or da, key=lambda d: d[0]) if (cos or da) else None
    if this_filed:
        rows.append(("filing date", f"this filing dated/served {this_filed[1]}", f"{aname}:{lineno(A, this_filed[2])}", "OK"))
        seen = set()
        for d in sorted(dr, key=lambda d: d[0], reverse=True):
            if d[0] >= this_filed[0] or d[1] in seen: continue
            seen.add(d[1]); gap = (this_filed[0] - d[0]).days
            if gap <= 60:
                rows.append(("day gap", f"{gap} days after record date {d[1]} ({R[max(0, d[2]-50):d[2]].strip().splitlines()[-1][-50:] if R[max(0, d[2]-50):d[2]].strip() else 'record'})", f"{rname}:{lineno(R, d[2])}", "VERIFY rule window"))
    # 5. signature block / counsel
    sig = re.findall(r"/s/ ?([A-Z][A-Za-z.' -]+)", A); ids = re.findall(r"(?:PA|Pa\.|Bar|Attorney) ?I\.?D\.? ?(?:No\.? ?)?(\d{4,6})", A)
    rows.append(("signature", f"signed /s/ {', '.join(dict.fromkeys(s.strip() for s in sig)) or 'NONE'}; attorney IDs {', '.join(dict.fromkeys(ids)) or 'NONE'}", aname, "OK" if sig else "DEFECT"))
    # 6. how it designates the opposing party (sui juris signer must never be relabelled)
    for m in re.finditer(r"(?i)\bpro[- ]?se\b[^\n]{0,30}", A):
        rows.append(("party designation", f'filing labels opposing party "{m.group(0).strip()}"', f"{aname}:{lineno(A, m.start())}", "DEFECT if signer is sui juris (check record)"))
    # 7. verification / oath
    rows.append(("verification", "verification/affidavit present" if re.search(r"(?i)\bverif(ication|ied)\b|\baffidavit\b|\b18 Pa\.C\.S\. ?§ ?4904\b|\b28 U\.S\.C\. ?§ ?1746\b", A) else "NO verification/oath language in filing", aname, "OK" if re.search(r"(?i)\bverif(ication|ied)\b|\baffidavit\b", A) else "VERIFY required for this filing type?"))
    # 7b. (2026-11-11) what the paper DEMANDS of us, on its face — the lane decides the rule's effect (notice-to-plead / notice-to-defend /
    #     reply-required); rows are facts about the face, agnostic to court: a pleading that carries no notice cannot start a response clock.
    # the paper's TITLE = the first heading line (all caps, standalone) in the caption block that names a paper type; the body
    # may mention every kind ("Answer", "Order", "Complaint") so the body never decides.
    heads = [l.strip() for l in A[:6000].splitlines() if l.strip() and l.strip() == l.strip().upper() and 4 <= len(l.strip()) <= 90]
    title = next((h for h in heads if re.search(r"\b(COMPLAINT|ANSWER|NEW MATTER|COUNTERCLAIM|REPLY|ORDER|OPINION|BRIEF|MOTION|OBJECTIONS|PETITION|APPLICATION|RESPONSE|NOTICE)\b", h)
                  and not re.search(r"COURT|DIVISION|COUNTY|TERM|CONTROL|CASE|DOCKET|NO\.", h)), "")
    if re.fullmatch(r"NOTICE( TO DEFEND)?", title) or not title:        # a complaint opens with its Notice-to-Defend page: look past it for the title
        title = next((h for h in heads if re.search(r"\bCOMPLAINT\b", h) and not re.search(r"\b(ANSWER|OBJECTIONS|TO THE)\b", h)), title)
    kind = ("complaint" if re.search(r"\bCOMPLAINT\b", title) and not re.search(r"\b(ANSWER|OBJECTIONS|TO)\b", title) else
            "pleading" if re.search(r"\b(ANSWER|NEW MATTER|COUNTERCLAIM|REPLY TO NEW MATTER)\b", title) else
            "order" if re.search(r"\b(ORDER|OPINION)\b", title) else "brief/motion")
    rows.append(("paper kind", f"face reads as: {kind} (title line: \"{title[:60] or 'none found'}\")", f"{aname}:1-60", "OK"))
    ntp = re.search(r"(?i)notice to plead|hereby notified to (?:file|plead)|within twenty \(?20\)? days|within 20 days", A)
    ntd = re.search(r"(?i)notice to defend|you have been sued in court|AVISO", A)
    if kind == "complaint":
        rows.append(("notice to defend", "notice to defend present" if ntd else "NO notice-to-defend language", f"{aname}:{lineno(A, ntd.start()) if ntd else '—'}", "OK" if ntd else "DEFECT (Rule 1018.1 — verify forum)"))
    elif kind == "pleading":
        rows.append(("notice to plead", "notice to plead present" if ntp else "NO notice-to-plead language anywhere in the paper", f"{aname}:{lineno(A, ntp.start()) if ntp else 'all pages'}",
                     "VERIFY response clock runs" if ntp else "VERIFY: no response required? averments deemed denied? (Rule 1026(a)/1029(d)/1361 note — lane decides)"))
        rows.append(("response posture", f"paper asks a response from us: {'YES (notice endorsed)' if ntp else 'NOT on its face'}; order recital pleaded as averment: {'YES' if re.search(r'(?i)by order of court|were dismissed|was dismissed|court sustained', A) else 'no'}",
                     aname, "VERIFY what a reply admits / what silence admits (1029(b),(d))"))
    exh_ref = sorted(set(re.findall(r"(?i)\bExhibit\s+\"?([A-Z]|\d{1,2})\b", A))); exh_tab = sorted(set(re.findall(r"(?m)^\s*EXHIBIT\s*\n?\s*([A-Z]|\d{1,2})\s*$", A)))
    if exh_ref:
        miss = [e for e in exh_ref if e not in exh_tab]
        rows.append(("exhibits attached", f"referenced {exh_ref}; tab pages found {exh_tab or 'NONE'}; missing {miss or 'none'}", aname, "DEFECT (referenced but not attached)" if miss else "OK"))
    # 8. characterisations of the opposing filing (prima facie material from their own words)
    for m in re.finditer(r"(?i)[^.\n]*\b(artificial intelligence|AI-generated|non-responsive|frivolous|incomprehensible|sovereign|pseudo)[^.\n]*\.", A):
        rows.append(("their own words", m.group(0).strip()[:220], f"{aname}:{lineno(A, m.start())}", "QUOTE (prima facie)"))
    # 9. page/word limits proxy
    pages = A.count("\f") + 1; words = len(A.split())
    rows.append(("length", f"~{pages} pages, {words} words", aname, "VERIFY forum limit"))
    with open(out, "w") as f:
        f.write(f"# FACE TABLE — face-of-filing facts for {aname} (deterministic; file:line evidence; lanes apply the forum's rules)\n")
        f.write("Form: FACE: <check> | <finding> | <evidence> | <status>\n\n")
        for r in rows: f.write("FACE: " + " | ".join(r) + "\n")
    return {"rows": len(rows), "defects": sum(r[3].startswith("DEFECT") for r in rows), "file": out}

if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1], sys.argv[2], sys.argv[3])))
