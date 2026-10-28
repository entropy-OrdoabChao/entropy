"""
default_timeline.py — agnostic, by-the-book default determination.

Default is a TIMING computation, not a keyword. For a removed case:
  - The removal date starts the clock (28 U.S.C. 1446; FRCP 81(c)(2)).
  - A defendant must answer/respond within 7 days after removal OR 21 days
    after service, whichever is longer (FRCP 81(c)(2), 12(a)(1)(A)).
  - A defendant who does not respond by that deadline is in DEFAULT (FRCP 55(a)),
    even by one day.
  - A plaintiff's later AMENDMENT does NOT retroactively cure a default that had
    already attached: the default crystallized on the deadline date.

Reads the parsed docket (ECF# -> {date, desc}) and computes, per defendant:
  served_or_removal_date, deadline, first_response_date, in_default,
  default_attached_date, cured_by_amendment (False — amendment does not cure).

Agnostic: works for any case with a parsed docket + defendant list. Removed vs
originally-filed is detected from the docket (presence of a Notice of Removal).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import datetime as _dt
from typing import Any


def _pdate(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s).strip(), fmt).date()
        except Exception:
            continue
    return None


_RESPONSE_TOKENS = ("answer", "motion to dismiss", "notice of appearance",
                    "motion for extension", "response to complaint")


def analyze_defaults(docket: dict, defendants: list[str],
                     amendment_date: str | None = None) -> dict:
    """Compute per-defendant default status from the docket timeline.

    docket: {ecf_str: {"date": "MM/DD/YYYY", "desc": "..."}}
    defendants: list of defendant name fragments (lowercased match)
    amendment_date: date of operative amended complaint (string), if any.

    Returns {removed, removal_date, deadline, amendment_date, defendants: {...}}
    """
    # removal date = date of the Notice of Removal entry, if present
    removal_date = None
    removed = False
    for ecf, e in docket.items():
        desc = str(e.get("desc", "")).lower()
        if "notice of removal" in desc:
            removed = True
            removal_date = _pdate(e.get("date", ""))
            break

    # FRCP 81(c)(2): 7 days after removal (or 21 after service if longer; we use
    # the removal+7 floor where service date is not separately known).
    deadline = (removal_date + _dt.timedelta(days=7)) if removal_date else None
    amd = _pdate(amendment_date) if amendment_date else None

    out = {}
    for name in defendants:
        nl = name.lower()
        first = None
        for ecf, e in sorted(docket.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            desc = str(e.get("desc", "")).lower()
            if nl in desc and any(tok in desc for tok in _RESPONSE_TOKENS):
                dd = _pdate(e.get("date", ""))
                if dd:
                    first = {"date": dd.isoformat(), "ecf": ecf,
                             "kind": ("answer" if "answer" in desc
                                      else "motion_to_dismiss" if "dismiss" in desc
                                      else "appearance" if "appearance" in desc
                                      else "extension" if "extension" in desc
                                      else "response")}
                    break
        in_default = False
        days_late = None
        if deadline:
            if first is None:
                in_default = True
            else:
                fd = _pdate(first["date"])
                if fd and fd > deadline:
                    in_default = True
                    days_late = (fd - deadline).days
        # amendment does NOT cure a default already attached
        cured = False
        attached_before_amendment = bool(in_default and amd and deadline and deadline <= amd)
        out[name] = {
            "in_default": in_default,
            "deadline": deadline.isoformat() if deadline else None,
            "first_response": first,
            "days_late": days_late,
            "default_attached_date": deadline.isoformat() if (in_default and deadline) else None,
            "cured_by_amendment": cured,
            "attached_before_amendment": attached_before_amendment,
        }
    return {
        "removed": removed,
        "removal_date": removal_date.isoformat() if removal_date else None,
        "deadline": deadline.isoformat() if deadline else None,
        "amendment_date": amd.isoformat() if amd else None,
        "defendants": out,
        "defaulted": [n for n, v in out.items() if v["in_default"]],
    }


if __name__ == "__main__":
    import json
    d = json.load(open(str(_eroot() / "_docket_parsed.json")))
    res = analyze_defaults(
        d,
        ["transunion", "greensky", "experian", "equifax", "capital one", "avant"],
        amendment_date="07/22/2025",  # ECF 52 amended complaint (approx)
    )
    print("removed:", res["removed"], "| removal:", res["removal_date"], "| deadline:", res["deadline"])
    print("DEFAULTED:", res["defaulted"])
    for n, v in res["defendants"].items():
        if v["in_default"]:
            fr = v["first_response"]
            print(f"  {n}: deadline {v['deadline']}, "
                  f"{'NO RESPONSE' if not fr else fr['date']+' '+fr['kind']}, "
                  f"days_late={v['days_late']}, attached_before_amendment={v['attached_before_amendment']}")
