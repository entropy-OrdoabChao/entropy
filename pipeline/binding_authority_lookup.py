"""
pipeline/binding_authority_lookup.py
=====================================
ChromaDB-driven binding authority lookup.

For each legal issue surfaced in the scorecard, query the legal_docs
ChromaDB collection (3.1M chunks) for SCOTUS + governing-circuit
precedential decisions that ALREADY DECIDED the issue. Surface these
in the scorecard so the court sees the binding precedent on each point.

PUBLIC API
----------
binding_authorities_for_issues(issues, jurisdiction='3d Cir') -> Dict[str, List[Dict]]

Returns mapping of issue -> [{citation, court, year, holding, doc_id}, ...]
Each result is a binding-precedent citation with the holding paraphrased.
"""
from __future__ import annotations

import re
import logging
from typing import Dict, List, Any, Optional

log = logging.getLogger("binding_authority_lookup")


# Hand-curated seed cases per issue (used as fallback when ChromaDB is
# unavailable, AND for results-enrichment when ChromaDB returns chunks).
# Each entry: (citation, court, year, holding)
SEED_BINDING_AUTHORITIES = {
    "Pleading Standard": [
        ("Bell Atl. Corp. v. Twombly, 550 U.S. 544 (2007)", "SCOTUS", 2007,
         "A complaint must plead enough facts to state a claim for relief that is plausible on its face."),
        ("Ashcroft v. Iqbal, 556 U.S. 662 (2009)", "SCOTUS", 2009,
         "The plausibility standard requires more than a sheer possibility that a defendant has acted unlawfully."),
        ("Phillips v. County of Allegheny, 515 F.3d 224 (3d Cir. 2008)", "3d Cir.", 2008,
         "Under Twombly, the court must accept all well-pleaded factual allegations as true and draw all reasonable inferences in favor of the plaintiff."),
        ("Connelly v. Lane Construction Corp., 809 F.3d 780 (3d Cir. 2016)", "3d Cir.", 2016,
         "Three-step analysis under Iqbal: (1) take note of elements; (2) identify conclusory allegations not entitled to assumption of truth; (3) assume veracity of well-pleaded factual allegations."),
    ],
    "Pro Se Construction": [
        ("Haines v. Kerner, 404 U.S. 519 (1972)", "SCOTUS", 1972,
         "Pro se pleadings are held to less stringent standards than formal pleadings drafted by lawyers."),
        ("Erickson v. Pardus, 551 U.S. 89 (2007)", "SCOTUS", 2007,
         "A document filed pro se is to be liberally construed."),
        ("Estelle v. Gamble, 429 U.S. 97 (1976)", "SCOTUS", 1976,
         "Pro se complaints, however inartfully pleaded, must be held to less stringent standards."),
        ("Mala v. Crown Bay Marina, Inc., 704 F.3d 239 (3d Cir. 2013)", "3d Cir.", 2013,
         "Pro se litigants are afforded liberal construction of their pleadings but are not exempt from the rules of procedure."),
    ],
    "Service": [
        ("Henderson v. United States, 517 U.S. 654 (1996)", "SCOTUS", 1996,
         "Service of process is governed by FRCP 4; the rule is to be liberally construed."),
        ("Murphy Bros. v. Michetti Pipe Stringing, 526 U.S. 344 (1999)", "SCOTUS", 1999,
         "Formal service of process triggers the defendant's obligation to respond."),
        ("Bullock v. United States, 763 F.2d 81 (3d Cir. 1985)", "3d Cir.", 1985,
         "Where the defendant has received actual notice and there has been substantial compliance with the rules, service is sufficient."),
    ],
    "Notice": [
        ("Mullane v. Cent. Hanover Bank & Trust Co., 339 U.S. 306 (1950)", "SCOTUS", 1950,
         "Notice must be reasonably calculated, under all the circumstances, to apprise interested parties of the pendency of the action."),
        ("Mathews v. Eldridge, 424 U.S. 319 (1976)", "SCOTUS", 1976,
         "Procedural due process is determined by the three-factor balancing test of private interest, risk of erroneous deprivation, and government interest."),
        ("Goldberg v. Kelly, 397 U.S. 254 (1970)", "SCOTUS", 1970,
         "The fundamental requisite of due process of law is the opportunity to be heard."),
    ],
    "Mailbox Presumption / Prima Facie": [
        ("Rosenthal v. Walker, 111 U.S. 185 (1884)", "SCOTUS", 1884,
         "Letters properly addressed, stamped, and mailed are presumed to have been received in due course of mail."),
        ("Hagner v. United States, 285 U.S. 427 (1932)", "SCOTUS", 1932,
         "The presumption of receipt arises from proof of proper mailing; it can be rebutted only by competent evidence of non-receipt."),
        ("Lupyan v. Corinthian Colleges Inc., 761 F.3d 314 (3d Cir. 2014)", "3d Cir.", 2014,
         "A strong presumption of receipt applies when notice is sent via certified mail."),
    ],
    "Standing": [
        ("Lujan v. Defenders of Wildlife, 504 U.S. 555 (1992)", "SCOTUS", 1992,
         "Standing requires (1) concrete and particularized injury in fact, (2) causation, and (3) redressability."),
        ("Spokeo, Inc. v. Robins, 578 U.S. 330 (2016)", "SCOTUS", 2016,
         "A plaintiff must have suffered a concrete injury for Article III standing, even when a statutory violation has occurred."),
        ("TransUnion LLC v. Ramirez, 141 S. Ct. 2190 (2021)", "SCOTUS", 2021,
         "Every class member must demonstrate Article III standing to recover individual damages."),
    ],
    "Jurisdiction": [
        ("Steel Co. v. Citizens for a Better Env't, 523 U.S. 83 (1998)", "SCOTUS", 1998,
         "Subject-matter jurisdiction is a threshold question; without it, the court cannot proceed."),
        ("Arbaugh v. Y&H Corp., 546 U.S. 500 (2006)", "SCOTUS", 2006,
         "Subject-matter jurisdiction can never be forfeited or waived."),
    ],
    "Void Judgment": [
        ("United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260 (2010)", "SCOTUS", 2010,
         "Rule 60(b)(4) relief is appropriate only where the court that entered the judgment lacked jurisdiction or where the judgment was entered in violation of due process."),
        ("Marshall v. Bd. of Educ., 575 F.2d 417 (3d Cir. 1978)", "3d Cir.", 1978,
         "A void judgment is a legal nullity; relief from a void judgment is available at any time."),
    ],
    "Hierarchy / Stare Decisis": [
        ("Hutto v. Davis, 454 U.S. 370 (1982) (per curiam)", "SCOTUS", 1982,
         "Lower courts are bound to follow the decisions of the Supreme Court of the United States."),
        ("Rodriguez de Quijas v. Shearson/Am. Express, 490 U.S. 477 (1989)", "SCOTUS", 1989,
         "If a precedent of the Supreme Court has direct application, the Court of Appeals should follow it."),
        ("3d Cir. I.O.P. 5.7", "3d Cir.", 2007,
         "Not-precedential opinions of the Third Circuit are not regarded as precedents that bind the Court."),
    ],
    "Evidence (FRE 201)": [
        ("Pension Benefit Guar. Corp. v. White Consol. Indus., 998 F.2d 1192 (3d Cir. 1993)", "3d Cir.", 1993,
         "Courts may take judicial notice of public records."),
        ("Werner v. Werner, 267 F.3d 288 (3d Cir. 2001)", "3d Cir.", 2001,
         "Judicial notice of court records is appropriate; Rule 201(c)(2) makes such notice mandatory upon proper request."),
    ],
    "Verification": [
        ("Detrick v. Pa. R.R., 130 A.2d 142 (Pa. 1957)", "Pa. Supreme", 1957,
         "Pa.R.C.P. 1024 requires verification of factual averments; absence renders averments deemed admitted."),
    ],
    "Deadlines": [
        ("Bowles v. Russell, 551 U.S. 205 (2007)", "SCOTUS", 2007,
         "Statutory time limits for taking an appeal are jurisdictional."),
    ],
    "Judicial Conduct": [
        ("Liljeberg v. Health Servs. Acquisition Corp., 486 U.S. 847 (1988)", "SCOTUS", 1988,
         "28 U.S.C. § 455(a) requires recusal whenever the judge's impartiality might reasonably be questioned."),
        ("Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009)", "SCOTUS", 2009,
         "Due process requires recusal where the probability of actual bias rises to an unconstitutional level."),
        ("Liteky v. United States, 510 U.S. 540 (1994)", "SCOTUS", 1994,
         "Judicial rulings alone almost never constitute valid basis for recusal motion; bias must come from an extrajudicial source."),
    ],
    "Civil Rights (Title 18/42)": [
        ("Screws v. United States, 325 U.S. 91 (1945)", "SCOTUS", 1945,
         "18 U.S.C. § 242 criminalizes willful deprivations of constitutional rights under color of law."),
        ("Monroe v. Pape, 365 U.S. 167 (1961)", "SCOTUS", 1961,
         "42 U.S.C. § 1983 provides a civil remedy for deprivations of constitutional rights under color of law."),
        ("West v. Atkins, 487 U.S. 42 (1988)", "SCOTUS", 1988,
         "Acting under color of state law requires that the defendant exercised power possessed by virtue of state law."),
    ],
}


def _try_chroma_search(issue: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Best-effort ChromaDB lookup for binding authority on an issue.
    Returns [] if ChromaDB is unavailable or returns nothing usable."""
    try:
        import chromadb
        client = chromadb.HttpClient(host="localhost", port=8100)
        col = client.get_collection("legal_docs")
    except Exception as e:
        log.debug("chromadb unavailable: %s", e)
        return []

    # Compose a query keyed to binding authority
    queries = {
        "Pleading Standard": "Twombly Iqbal plausibility standard pleading sufficient facts",
        "Pro Se Construction": "Haines Kerner Erickson Pardus pro se liberal construction",
        "Service": "FRCP 4 service of process sufficiency",
        "Notice": "Mullane due process notice opportunity to be heard",
        "Mailbox Presumption / Prima Facie": "Rosenthal Hagner mailbox presumption properly addressed mail",
        "Standing": "Lujan Spokeo TransUnion Ramirez Article III standing injury",
        "Jurisdiction": "Steel Co Arbaugh subject matter jurisdiction Rule 12(h)(3)",
        "Void Judgment": "Espinosa Rule 60(b)(4) void judgment lack of jurisdiction",
        "Hierarchy / Stare Decisis": "stare decisis binding precedent IOP 5.7 unpublished disposition",
        "Evidence (FRE 201)": "judicial notice FRE 201(b)(2) public records",
        "Verification": "Pa.R.C.P. 1024 verification deemed admissions",
        "Deadlines": "FRAP 4 FRAP 26 jurisdictional time limit appeal",
        "Judicial Conduct": "28 USC 455 disqualification recusal Liljeberg Caperton",
        "Civil Rights (Title 18/42)": "color of law 1983 deprivation rights Screws",
    }
    query = queries.get(issue, issue)
    try:
        result = col.query(query_texts=[query], n_results=top_k)
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        out = []
        for i, d in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            # Try to extract any citation strings from the chunk
            cites = re.findall(
                r"[A-Z][A-Za-z\. ]+\s+v\.\s+[A-Z][A-Za-z\. ]+,\s+\d+\s+(?:U\.S\.|F\.\d?d?|S\.\s*Ct\.)\s+\d+",
                d or "",
            )
            for cite in cites[:2]:
                court = "SCOTUS" if "U.S." in cite else ("3d Cir." if "3d Cir" in cite else "Other")
                year_m = re.search(r"\((\d{4})\)", cite)
                year = int(year_m.group(1)) if year_m else 0
                out.append({
                    "citation": cite.strip(),
                    "court": court,
                    "year": year,
                    "holding": (d[:240] + "...") if d and len(d) > 240 else (d or ""),
                    "doc_id": meta.get("doc_id", ""),
                    "source": "chromadb",
                })
        return out
    except Exception as e:
        log.debug("chromadb query failed: %s", e)
        return []


def binding_authorities_for_issues(
    issues: List[str],
    jurisdiction: str = "3d Cir",
    enrich_from_db: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return a dict of {issue: [authority, ...]} for use in the scorecard.

    Always returns the hand-curated seed cases; optionally appends extra
    chunks from ChromaDB.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        seeds = SEED_BINDING_AUTHORITIES.get(issue, [])
        authorities = [
            {
                "citation": c,
                "court": court,
                "year": year,
                "holding": holding,
                "source": "seed",
            }
            for (c, court, year, holding) in seeds
        ]
        if enrich_from_db:
            extras = _try_chroma_search(issue, top_k=5)
            # Dedupe by citation prefix
            seen = {a["citation"].split(",")[0].strip() for a in authorities}
            for e in extras:
                key = e["citation"].split(",")[0].strip()
                if key not in seen:
                    seen.add(key)
                    authorities.append(e)
        out[issue] = authorities
    return out


if __name__ == "__main__":
    import sys
    issues = sys.argv[1:] or list(SEED_BINDING_AUTHORITIES.keys())
    result = binding_authorities_for_issues(issues, enrich_from_db=False)
    for issue, auths in result.items():
        print(f"\n=== {issue} ({len(auths)} authorities) ===")
        for a in auths[:5]:
            print(f"  - {a['citation']}")
            print(f"    {a['court']} ({a['year']}): {a['holding'][:120]}")
