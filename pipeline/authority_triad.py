# -*- coding: utf-8 -*-
"""
authority_triad — the missing SYNTHESIS layer. The three legs of a court's authority —
CAPACITY, STANDING, JURISDICTION — are already DETECTED and land in the audit JSON as
separate findings (jurisdiction_check -> jurisdictional_defects; block14 -> procedural_violations
incl. Lujan standing + capacity/verification). Nothing reasons over them TOGETHER.

This does: it reads the findings already in the audit, classifies them into the three legs, and
returns ONE conclusion — authority present/absent, which leg failed, and (if any failed) that
WITHOUT ALL THREE THERE IS NO AUTHORITY and the act/judgment is void. It DETECTS nothing new; it
synthesizes what the detectors already found. Corpus/audit decide — no assertion.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
AUDIT = ROOT / "output" / "audit"

# classifiers over EXISTING audit findings -> the 9 threshold legs (Black's-grounded taxonomy).
_CLS = {
    "personal_jurisdiction": re.compile(r"personal jurisdiction|minimum contacts|long.?arm|in personam|"
                                        r"in rem|quasi.?in.?rem|International Shoe", re.I),
    "standing": re.compile(r"\bstanding\b|injury.?in.?fact|redressab|concrete.{0,15}particulariz", re.I),
    "ripeness": re.compile(r"ripe(ness)?|not ripe|fit for judicial|prematur", re.I),
    "mootness": re.compile(r"moot(ness)?|no longer.{0,20}(controversy|live)|academic|dead controversy", re.I),
    "capacity": re.compile(r"capacity|verification|verified|unverified|1024|1029|charter|corporate existence|"
                           r"power to (sue|bring)|authority to (sue|act)", re.I),
    "real_party_in_interest": re.compile(r"real party in interest|rule 17|pa\.?r\.?c\.?p\.? ?2002|"
                                         r"entitled to enforce|assignee.{0,20}(no|without) (right|standing)", re.I),
    "venue": re.compile(r"\bvenue\b|improper venue|1006|1391|wrong (county|district)", re.I),
    "service_of_process": re.compile(r"service of process|defective service|insufficien.{0,10}service|"
                                     r"\bquash\b|rule 4\b|pa\.?r\.?c\.?p\.? ?440|not (properly )?served", re.I),
}

# grounding per leg (all corpus/Black's-backed authority)
_LEG_LAW = {
    "subject_matter_jurisdiction": "SMJ is a prerequisite; without it any judgment is void and the case must be "
                    "remanded (a court cannot create jurisdiction through its own mistakes).",
    "personal_jurisdiction": "Personal jurisdiction requires minimum contacts + due process (long-arm; "
                    "International Shoe; Ford Motor Co. v. Montana); defective service defeats it. In personam / "
                    "in rem / quasi in rem (Black's).",
    "standing": "Lujan v. Defenders of Wildlife, 504 U.S. 555 (1992): (1) concrete & particularized injury in "
                "fact, (2) causation, (3) redressability.",
    "ripeness": "Ripeness (Black's): the claim must be 'fit for judicial decision' — not premature; an unripe "
                "claim is non-justiciable.",
    "mootness": "Mootness (Black's): a court will not decide moot cases — those in which there is no longer a "
                "live controversy; the issue has become academic or dead.",
    "capacity": "Capacity follows the act (Planters' Bank 22 U.S. 904; Clearfield 318 U.S. 363 — the actor "
                "takes the character of the business transacted); pleadings must be brought by a party with "
                "capacity and verified (Pa.R.C.P. 1024).",
    "real_party_in_interest": "FRCP 17(a)(1) / Pa.R.C.P. 2002: an action must be prosecuted in the name of the "
                "real party in interest — the person with the legal right sought to be enforced.",
    "venue": "Venue must be proper (Pa.R.C.P. 1006 / 28 U.S.C. § 1391); improper venue is a Rule 12(b)(3) "
             "/ 1028 defect.",
    "service_of_process": "Valid service of process is required for jurisdiction over the person (FRCP 4 / "
             "Pa.R.C.P. 440); defective or absent service defeats personal jurisdiction (motion to quash).",
}
_JUSTICIABILITY = ("standing", "ripeness", "mootness")
_JURISDICTION = ("subject_matter_jurisdiction", "personal_jurisdiction")


def _load(slug: str):
    for name in (f"data_{slug}.json", f"data_{slug.replace(':','_').replace('.','_')}.json"):
        p = AUDIT / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")), p
            except Exception:
                pass
    # loose match
    for p in AUDIT.glob("data_*.json"):
        if slug.replace(":", "_").replace(".", "_") in p.name:
            try:
                return json.loads(p.read_text(encoding="utf-8")), p
            except Exception:
                pass
    return None, None


def analyze(slug: str) -> dict:
    audit, _ = _load(slug)
    if audit is None:
        return {"slug": slug, "status": "no_audit", "authority_present": None}
    jx = audit.get("jurisdictional_defects", []) or []
    pv = audit.get("procedural_violations", []) or []
    def _txt(v):
        # jurisdictional_defects use {type, why}; procedural_violations use {violation_type, description}
        if isinstance(v, dict):
            return " ".join(str(v.get(k, "")) for k in
                            ("violation_type", "description", "type", "why", "reason", "explanation", "rule"))
        return str(v)
    pool = list(jx) + list(pv)                       # classify ALL findings into the 9 legs

    legs = {}
    for leg in _LEG_LAW:
        if leg == "subject_matter_jurisdiction":
            found = [v for v in jx if not _CLS["personal_jurisdiction"].search(_txt(v))]
        else:
            cls = _CLS.get(leg)
            found = [v for v in pool if cls and cls.search(_txt(v))]
        legs[leg] = {"failed": bool(found), "findings": found, "law": _LEG_LAW[leg]}

    failed = [k for k, v in legs.items() if v["failed"]]
    present = not failed
    return {
        "slug": slug,
        "authority_present": present,
        "failed_legs": failed,
        "void": bool(failed),
        "why": ("basis of authority intact: all threshold elements (jurisdiction [SMJ + personal], "
                "justiciability [standing/ripeness/mootness], capacity, real party in interest, venue, "
                "service) satisfied on the record"
                if present else
                "basis of authority BROKEN — failed leg(s): " + ", ".join(failed) +
                ". These are the threshold elements of a court's authority; if any one fails there is no "
                "authority and the act/judgment is void."),
        "legs": legs,
    }


def write_audit(slug: str) -> dict:
    audit, p = _load(slug)
    if audit is None:
        return {"slug": slug, "status": "no_audit"}
    r = analyze(slug)
    audit["authority_triad"] = {k: r[k] for k in ("authority_present", "failed_legs", "void", "why", "legs")}
    p.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    return r


if __name__ == "__main__":
    import sys, glob
    slugs = sys.argv[1:] or [Path(f).name[5:-5] for f in sorted(glob.glob(str(AUDIT / "data_*.json")))]
    for s in slugs:
        r = analyze(s)
        if r.get("authority_present") is None:
            continue
        print(f"{s}: authority_present={r['authority_present']} void={r['void']} "
              f"failed={r['failed_legs'] or 'none'}")
        print("   " + r["why"][:120])
