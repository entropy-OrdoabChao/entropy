#!/usr/bin/env python3
"""ground_order.py — the CONSTITUTIONAL-AVOIDANCE ORDERING canon as a composer organ.

WHY (class-level, every case, every forum): a court disposes of a case on the NARROWEST ground that
ends it. A filing that leads with the constitutional question invites the court to avoid the whole
filing; a filing that leads with service / jurisdiction / pendency / standing gives the court a ground
it is *required* to reach first, and keeps the constitutional question alive as the thing it CANNOT
avoid if it refuses the narrow grounds. That is not strategy — it is the court's own rule:

  [corpus:cap_us_557_193_northwest_austin_municipal_utility_distr#15d24e41ff]
    "To the extent that constitutional avoidance is a worthwhile tool of statutory construction, it is
     because it allows a court to dispose of an entire case on grounds that do not require the court to
     pass on a statute's constitutionality. See Ashwander v. TVA, 297 U. S. 288, 347 (1936) (Brandeis,
     J., concurring) ("The Court will not pass upon a constitutional question although properly
     presented by the record, if there is also present some other ground upon which the case may be
     disposed of")"
  [corpus:cap_us_529_473_slack_v_mcdaniel#71d0c2e40d]
    "The Ashwander rule should inform the court'..." / "allows and encourages the court to first resolve
     procedural issues."                                  <- THE ORDERING authority (procedural first)
  [corpus:scotus_stilp_v_commonwealth_905a2d918#165032b384]   PA counterpart (Ballou v. State Ethics
    Comm'n, 496 Pa. 127, 436 A.2d 186, 187 (1981)): "[W]hen a case raises both constitutional and
    non-constitutional issues, a court should not reach the constitutional issue if the case can
    properly be decided on non-constitutional grounds."
  [corpus:amjur2d_16_constitutional_law#1ccc816639]  16 Am Jur 2d Constitutional Law § 172: "tion of a
    constitutional question must be reserved to the last, when it is imperatively required or
    unavoidable." / "The court will refuse to decide constitutional questions when the record discloses
    other grounds for decision, whether or not such other grounds have been properly raised"
  [corpus:amjur2d_16_constitutional_law#bedae2d5ed]  § 221, the CONSTRUCTION side of the canon (and its
    limit): "a statute must be construed, if fairly possible, so as to avoid doubts as to its
    constitutionality, will not be pressed to the point of disingenuous evasion where the legislative
    intention is distinctly revealed."
  [corpus:cap_us_508_602_concrete_pipe_products_of_california_inc#bb063b0132]
    "Federal statutes are to be so construed as to avoid serious doubt of their constitutionality."

TWO ORGANS, ONE CANON:
  (1) ORDERING  — grounds are emitted narrow -> broad and the constitutional ground is ALWAYS LAST,
      as a BACKSTOP: "If the Court does not dispose of this matter on the grounds above, the
      constitutional question — <derived> — is squarely presented and cannot be avoided."
  (2) CONSTRUCTION — where a unit's rule is AMBIGUOUS, the reading that avoids the constitutional
      doubt is the one the court must adopt (§ 221 / Concrete Pipe). One STANDING INSTRUCTION for the
      composer/drafter prompt, applied where relevant, never everywhere.

DOCTRINE SPLIT (user-locked): deterministic = GUARDRAIL, LLM = COMPREHENSION.
  deterministic  the ground CLASS floor from the authority the unit actually rests on (cite_family.parse
                 -> kind: rule | statute | regulation | case), the constitutional-pin test, the
                 narrow->broad sort, "backstop last", and the refusal (OPEN + PULL:) when no
                 constitutional authority is GROUNDED in the cells.
  comprehension  WHICH class a unit belongs in when its authority family is ambiguous (a jurisdiction
                 statute is a procedural gate, not a compliance ground), and the backstop's own title
                 (which constitutional interest THESE units implicate). No keyword table decides either:
                 with no lane reachable the class falls back to the family floor and the backstop is
                 emitted OPEN with a PULL: line. A canned paragraph is NEVER emitted as if derived.

CLASSES (narrow -> broad)
  a  forum procedural gate       service, jurisdiction, pendency, standing/capacity, timeliness
  b  statutory / regulatory      compliance grounds under enacted text or a regulation
  c  common law / equity         case-law and equitable grounds with no enacted text of their own
  d  constitutional BACKSTOP     ALWAYS LAST; never pleaded as a lead ground

CLI (dry, read-only)
  python3 -m pipeline.m2.ground_order --cells <a.jsonl> [...] --units <UNITS.md> [--llm on|off] [--json out.json]
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "pipeline" / "m1.py").exists():
    ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sibling(name: str, mod_name: str):
    """Load a sibling organ FROM THE FILE NEXT TO THIS ONE (a sandbox copy gets its own), falling back to the
    live repo's copy. `pipeline` is a regular package, so a sandbox dir can NEVER go on sys.path — doing that
    shadows the whole live package (cite_family, the cite normalizer) and silently un-pins every cell. House
    pattern: cells_to_stub._cite_normalizer()."""
    p = Path(__file__).resolve().parent / name
    if not p.exists():
        p = ROOT / "pipeline" / "m2" / name
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ------------------------------------------------------------------ the canon, pinned (law, not case literals)
CLASSES = {
    "a": "forum procedural gate",
    "b": "statutory/regulatory compliance",
    "c": "common law / equity",
    "d": "constitutional backstop",
}
ORDER = ("a", "b", "c", "d")

AVOIDANCE_AUTHORITIES = [
    {"cite": "Northwest Austin Mun. Util. Dist. No. One v. Holder, 557 U.S. 193 (2009)",
     "chunk": "cap_us_557_193_northwest_austin_municipal_utility_distr#15d24e41ff",
     "quote": "constitutional avoidance is a worthwhile tool of statutory construction, it is because it allows a court to "
              "dispose of an entire case on grounds that do not require the court to pass on a statute's constitutionality"},
    {"cite": "Ashwander v. TVA, 297 U.S. 288, 347 (1936) (Brandeis, J., concurring)",
     "chunk": "cap_us_557_193_northwest_austin_municipal_utility_distr#15d24e41ff",
     "quote": "The Court will not pass upon a constitutional question although properly presented by the record, if there is "
              "also present some other ground upon which the case may be disposed of"},
    {"cite": "Slack v. McDaniel, 529 U.S. 473 (2000)",
     "chunk": "cap_us_529_473_slack_v_mcdaniel#71d0c2e40d",
     "quote": "allows and encourages the court to first resolve procedural issues"},
    {"cite": "16 Am. Jur. 2d Constitutional Law § 172",
     "chunk": "amjur2d_16_constitutional_law#1ccc816639",
     "quote": "The court will refuse to decide constitutional questions when the record discloses other grounds for decision, "
              "whether or not such other grounds have been properly raised before the court by the parties"},
]
# the forum counterpart: a PA trial court is bound by the PA Supreme Court's own statement of the canon
AVOIDANCE_BY_FORUM = {
    "PA": {"cite": "Ballou v. State Ethics Comm'n, 496 Pa. 127, 436 A.2d 186, 187 (1981), quoted in Stilp v. Commonwealth, 905 A.2d 918",
           "chunk": "scotus_stilp_v_commonwealth_905a2d918#165032b384",
           "quote": "[W]hen a case raises both constitutional and non-constitutional issues, a court should not reach the "
                    "constitutional issue if the case can properly be decided on non-constitutional grounds"},
}
CONSTRUCTION_AUTHORITIES = [
    {"cite": "16 Am. Jur. 2d Constitutional Law § 221",
     "chunk": "amjur2d_16_constitutional_law#bedae2d5ed",
     "quote": "a statute must be construed, if fairly possible, so as to avoid doubts as to its constitutionality"},
    {"cite": "Concrete Pipe & Products of Cal., Inc. v. Construction Laborers Pension Trust, 508 U.S. 602 (1993) (quoting Machinists v. Street, 367 U.S. 740, 749-750 (1961), and Crowell v. Benson, 285 U.S. 22, 62)",
     "chunk": "cap_us_508_602_concrete_pipe_products_of_california_inc#bb063b0132",
     "quote": "Federal statutes are to be so construed as to avoid serious doubt of their constitutionality"},
]

# (2) the STANDING INSTRUCTION — one line in the composer/drafter prompt, applied where a unit's rule is
#     ambiguous, never everywhere. Its own limit (§ 221: "not ... to the point of disingenuous evasion
#     where the legislative intention is distinctly revealed") travels with it so the drafter cannot use
#     the canon to rewrite plain text.
CONSTRUCTION_CANON_INSTRUCTION = (
    "AVOIDANCE CANON (construction tool — use ONLY where the unit's own rule text is genuinely ambiguous, "
    "never to rewrite plain text): where two readings of the rule are fairly possible and one of them would "
    "raise a constitutional doubt, plead the OTHER reading as the one the court must adopt, and say why in the "
    "unit's own words — \"a statute must be construed, if fairly possible, so as to avoid doubts as to its "
    "constitutionality\" [corpus:amjur2d_16_constitutional_law#bedae2d5ed]; \"Federal statutes are to be so "
    "construed as to avoid serious doubt of their constitutionality\" "
    "[corpus:cap_us_508_602_concrete_pipe_products_of_california_inc#bb063b0132]. The canon has a limit on the "
    "same page and you must respect it: it \"will not be pressed to the point of disingenuous evasion where the "
    "legislative intention is distinctly revealed\" [corpus:amjur2d_16_constitutional_law#bedae2d5ed] — if the "
    "text is plain, plead the text and carry the constitutional question to the BACKSTOP ground instead."
)

# ------------------------------------------------------------------ constitutional authority test (reads the CITE, not prose)
_CONST_RX = re.compile(
    r"(?:\b(?:U\.?\s?S\.?|United\s+States|Pa\.?|Pennsylvania|[A-Z][a-z]+)\s*Const(?:\.|itution)\b"
    r"|\bConst\.\s*(?:art|amend|Art|Amend)"
    r"|\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|"
    r"Fifteenth|Nineteenth)\s+Amendment\b"
    r"|\bamend\.\s*[IVXL]+\b"
    r"|\b(?:Due\s+Process|Equal\s+Protection|Takings|Contract|Supremacy)\s+Clause\b"
    r"|\bart\.\s*[IVX]+,\s*§)", re.I)
_CONST_SRC_RX = re.compile(r"(?:^|[_\-])constitution|_const(?:$|[_\-])|^us_const|^pa_const", re.I)


def is_constitutional(cite: str = "", source: str = "") -> bool:
    """True when THIS authority is constitutional text (or a holding pinned to a constitution shelf).
    Reads the citation / corpus source key — never the surrounding prose (prose says 'due process' all day)."""
    return bool(_CONST_RX.search(cite or "") or _CONST_SRC_RX.search(source or ""))


# ------------------------------------------------------------------ deterministic class FLOOR (from the authority family)
def _family_kind(cite: str) -> str:
    try:
        from pipeline.cite_family import parse as _parse      # PROMOTED organ (live repo) — imported, never forked
    except Exception:
        return "unknown"
    try:
        return (_parse(cite) or {}).get("kind") or "unknown"
    except Exception:
        return "unknown"


_KIND_CLASS = {"rule": "a", "statute": "b", "regulation": "b", "case": "c"}


def family_class(cite: str = "", source: str = "") -> str:
    """The class FLOOR this authority can support, derived from what KIND of law it is (cite_family.parse):
    court rule -> a (the forum's own gate), enacted text / regulation -> b, reported opinion -> c,
    constitutional text -> d. 'unknown' when the cite parses to nothing — the caller's guard decides."""
    if is_constitutional(cite, source):
        return "d"
    return _KIND_CLASS.get(_family_kind(cite), "unknown")


def deterministic_class(pins: list) -> dict:
    """-> {class, supported, why, families}.

    floor      the NARROWEST class any GROUNDED pin on this unit can support — the default when no
               comprehension lane answers. A unit that rests on a court rule can be disposed of at the rule.
    supported  EVERY class the unit's own pins can support. This is the guardrail comprehension must satisfy:
               a lane may call a unit 'statutory compliance' only if a statute/regulation is actually PINNED on
               it. (Why both: the room's preservation lanes staple a Pa.R.A.P. cite onto substantive units, so
               'narrowest pin' alone would file a 26 C.F.R. ground as a procedural gate. The pin set is the
               record; which of those pins IS the ground is comprehension.)"""
    fams = []
    for p in pins:
        c = family_class(p.get("cite") or "", p.get("chunk") or p.get("source") or "")
        fams.append({"cite": p.get("cite") or "", "chunk": p.get("chunk") or "", "class": c,
                     "kind": _family_kind(p.get("cite") or "")})
    have = [f["class"] for f in fams if f["class"] in ORDER]
    supported = [k for k in ("a", "b", "c") if k in have]
    narrow = supported[0] if supported else None
    if narrow:
        ex = next(f for f in fams if f["class"] == narrow)
        return {"class": narrow, "families": fams, "supported": supported,
                "why": f"narrowest GROUNDED authority is a {ex['kind']} ({ex['cite']}) -> class {narrow} ({CLASSES[narrow]})"}
    if "d" in have:
        return {"class": "d", "families": fams, "supported": [],
                "why": "every GROUNDED authority on this unit is constitutional text -> backstop"}
    return {"class": "unknown", "families": fams, "supported": [],
            "why": "no GROUNDED pin whose cite resolves to a family (rule/statute/regulation/case/constitution)"}


# ------------------------------------------------------------------ comprehension lane (bots GPU first; never the user's chat)
_LANES = [("http://localhost:8000/v1/chat/completions", "qwen3.8-27b-exl3"),      # local-gpu = m2 BOTS lane
          (os.environ.get("AVOIDANCE_LLM_URL", "http://localhost:8000/v1/chat/completions"), "qwen3.8-27b")]
LLM_LOG: list = []


def _llm(prompt: str, max_tokens: int = 900, timeout: int = 120) -> str:
    """One bounded chat call on the BOTS lane. Returns '' when no lane answers — the caller then refuses
    (OPEN + PULL:) instead of inventing. AVOIDANCE_LLM=off disables it loudly."""
    if (os.environ.get("AVOIDANCE_LLM") or "on").lower() == "off":
        LLM_LOG.append("AVOIDANCE_LLM=off — comprehension lane disabled by env")
        return ""
    body = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0.1}
    # SCHEMA-PINNED: the 27B lane answers "JSON only" with ~6,000 chars of prose and no object, and the whole
    # comprehension step then no-ops silently (observed twice). vLLM's guided decoding makes the answer BE the
    # object; a lane that rejects response_format is retried without it, so nothing depends on the server build.
    for guided in ({"response_format": {"type": "json_object"}}, {}):
        for url, model in _LANES:
            try:
                req = urllib.request.Request(url, data=json.dumps({**body, **guided, "model": model}).encode(),
                                             headers={"Content-Type": "application/json"})
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode())
                txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                if txt:
                    LLM_LOG.append(f"lane {url} ({model}{'/guided-json' if guided else ''}) answered in "
                                   f"{time.time()-t0:.1f}s, {len(txt)} chars")
                    return txt
                LLM_LOG.append(f"lane {url} returned empty content")
            except Exception as e:
                LLM_LOG.append(f"lane {url}{'/guided-json' if guided else ''} failed: "
                               f"{type(e).__name__}: {str(e)[:90]}")
    return ""


def _json_obj(txt: str) -> dict:
    """Pull the JSON object out of a chat answer. A 27B lane prefixes prose and sometimes fences the JSON, so a
    single greedy {...} match silently returns {} and the whole comprehension step no-ops (observed: a 5,900-char
    answer, zero classes applied). Try the fence, then every balanced brace span, longest first."""
    t = txt or ""
    cands = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    starts = [i for i, ch in enumerate(t) if ch == "{"]
    for i in starts:
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    cands.append(t[i:j + 1])
                    break
    for c in sorted(set(cands), key=len, reverse=True):
        try:
            o = json.loads(c)
            if isinstance(o, dict):
                return o
        except Exception:
            continue
    if t.strip():
        LLM_LOG.append(f"answer carried NO parseable JSON object ({len(t)} chars) — the floor stands")
    return {}


# ------------------------------------------------------------------ classification (floor + comprehension, guarded)
def classify_units(units: list, by_unit: dict, llm=None) -> dict:
    """-> {n: {class, source, why, floor, pins, const_pins}}.

    floor          deterministic, from the authority family (always computed, always reported)
    comprehension  may move a unit WITHIN {a,b,c} — a jurisdiction/timeliness statute is a procedural gate
                   even though its family is 'statute'. It may NOT move a unit to 'd' (the backstop is the
                   composer's own ground) and it may NOT move a unit out of 'a' (nothing is narrower than
                   the forum's gate). Unreachable lane / bad answer -> the floor stands, and we say so.
    """
    out = {}
    for u in units:
        n = u["n"]
        rows = by_unit.get(n) or []
        pins = [p for r in rows for p in (r.get("_pins") or [])]
        det = deterministic_class(pins)
        out[n] = {"class": det["class"], "source": "family-floor", "why": det["why"], "floor": det["class"],
                  "supported": det.get("supported") or [], "pins": pins, "families": det["families"],
                  "const_pins": [p for p in pins if is_constitutional(p.get("cite") or "", p.get("chunk") or "")],
                  "title": u.get("title") or ""}
    movable = [u["n"] for u in units if len(out[u["n"]]["supported"]) > 1]
    if not movable:
        return out
    ask = []
    for n in movable:
        rules = []
        seen_c = set()
        for p in out[n]["pins"]:
            c = (p.get("cite") or "").strip()
            if not c or c in seen_c:
                continue
            seen_c.add(c)
            q = re.sub(r"\s+", " ", (p.get("quote") or ""))[:160]
            rules.append({"cite": c, "class_it_can_support": family_class(c, p.get("chunk") or ""),
                          "quote": q} if q else {"cite": c, "class_it_can_support": family_class(c, p.get("chunk") or "")})
            if len(rules) >= 8:
                break
        ask.append({"unit": n, "title": re.sub(r"\s+", " ", out[n]["title"])[:240],
                    "authorities": rules, "classes_the_record_supports": out[n]["supported"],
                    "family_floor": out[n]["class"]})
    prompt = (
        "You are ordering the grounds of a court filing under the constitutional-avoidance canon: a court must "
        "dispose of a case on the NARROWEST ground that ends it, and reaches a constitutional question only if no "
        "other ground disposes of the matter.\n\n"
        "Classify each ground below into exactly one class, reading its authorities:\n"
        "  a = FORUM PROCEDURAL GATE — the court can end the matter here without reaching the merits: service/notice, "
        "subject-matter or personal jurisdiction, pendency of a prior action, standing / capacity / person entitled to "
        "enforce, timeliness, a defect in the pleading the forum's own rules require.\n"
        "  b = STATUTORY / REGULATORY COMPLIANCE — the opposing party failed a duty imposed by enacted text or a regulation.\n"
        "  c = COMMON LAW / EQUITY — the ground rests on reported decisions or equitable doctrine, with no enacted text of its own.\n"
        "Each ground lists the classes ITS OWN cited authorities can support ('classes_the_record_supports') and the "
        "'family_floor' (the narrowest of them). Pick the class that is the ground's real SUBSTANCE — the authority the "
        "court would actually dispose of it on. A preservation or appellate rule stapled onto a substantive ground does "
        "NOT make it a procedural gate; a jurisdiction, service, standing, pendency or timeliness statute IS class a even "
        "though its form is a statute. You may only pick a class listed in 'classes_the_record_supports'.\n"
        "Do NOT use class d — the constitutional ground is composed separately.\n\n"
        "GROUNDS:\n" + json.dumps(ask, indent=1)[:9000] + "\n\n"
        'Answer with JSON only: {"classes": [{"unit": <int>, "class": "a"|"b"|"c", "why": "<one short line naming the '
        'authority that decides it>"}]}')
    txt = (llm or _llm)(prompt, max_tokens=1600)
    got = _json_obj(txt).get("classes") or []
    for item in got if isinstance(got, list) else []:
        try:
            n = int(item.get("unit"))
        except Exception:
            continue
        c = str(item.get("class") or "").strip().lower()[:1]
        if n not in out or c not in ("a", "b", "c"):
            continue
        if out[n]["class"] == "d":
            continue                                       # the backstop is composed separately, never reclassified
        # DETERMINISTIC GUARD: the class comprehension picks must be one THIS unit's own GROUNDED pins can carry.
        # No pinned statute -> it cannot be a statutory ground, whatever the lane says.
        if c not in (out[n]["supported"] or []):
            out[n]["why"] += f" | comprehension said class {c}, REJECTED: no GROUNDED pin on this unit supports it"
            continue
        if c == out[n]["class"]:
            out[n]["source"] = "comprehension (agrees with family floor)"
            continue
        out[n].update({"class": c, "source": "comprehension",
                       "why": f"{re.sub(chr(10), ' ', str(item.get('why') or ''))[:180]} (family floor was {out[n]['floor']}; "
                              f"record supports {out[n]['supported']})"})
    return out


# ------------------------------------------------------------------ the BACKSTOP ground (always last)
def backstop_ground(units: list, by_unit: dict, classes: dict, forum: str = "", llm=None) -> dict:
    """-> {status: PLEAD|OPEN, title, interest, text, authorities, pull, source}.

    DETERMINISTIC GUARD (user-locked): the backstop must cite >= 1 constitutional authority already GROUNDED in
    the cells. With none, it is emitted OPEN with a PULL: line — never a canned constitutional paragraph."""
    const = []
    seen = set()
    for n, c in classes.items():
        for p in c.get("const_pins") or []:
            k = (p.get("cite"), p.get("chunk"))
            if k in seen:
                continue
            seen.add(k)
            const.append({**p, "unit": n})
    avoid = list(AVOIDANCE_AUTHORITIES)
    fk = (forum or "").upper()
    for pref, extra in AVOIDANCE_BY_FORUM.items():
        if pref in fk:
            avoid = [extra] + avoid
    if not const:
        units_named = sorted(n for n, c in classes.items() if c.get("class") in ("a", "b", "c", "d"))
        return {
            "status": "OPEN", "title": "", "interest": "", "source": "none",
            "authorities": avoid,
            "text": "",
            "pull": ("no constitutional authority is GROUNDED in the cells — the backstop ground cannot be pleaded. "
                     "PULL: the constitutional text and the holding the record's own units implicate (e.g. the due-process "
                     "guarantee the deprivation units rest on), ground it in a cell with a corpus chunk, then re-run. "
                     f"Units on file: {units_named}"),
        }
    # comprehension: WHICH constitutional interest THESE units implicate, in the court's own words.
    pack = []
    for n in sorted(classes):
        c = classes[n]
        t = re.sub(r"\s+", " ", c.get("title") or "")[:200]
        cp = "; ".join(f"{p.get('cite')}" for p in (c.get("const_pins") or [])[:3])
        pack.append({"unit": n, "class": c.get("class"), "ground": t, "constitutional_authority": cp})
    prompt = (
        "A court filing's grounds are ordered narrowest-first. The LAST ground is a BACKSTOP: it tells the court that if "
        "none of the earlier, non-constitutional grounds disposes of the matter, the constitutional question is squarely "
        "presented and cannot be avoided.\n\n"
        "From the grounds below (and ONLY from them), name the constitutional interest they implicate — in ONE clause, "
        "concrete, in the register of a court paper, naming what is taken and what was missing (e.g. 'deprivation of a home "
        "without service of process or proof of a person entitled to enforce'). Do not cite anything; do not add a ground "
        "that is not below; do not use the words 'constitutional question' in the clause itself.\n\n"
        "GROUNDS:\n" + json.dumps(pack, indent=1)[:9000] + "\n\n"
        'Answer with JSON only: {"interest": "<one clause>", "units": [<unit numbers the clause rests on>]}')
    txt = (llm or _llm)(prompt, max_tokens=500)
    obj = _json_obj(txt)
    interest = re.sub(r"\s+", " ", str(obj.get("interest") or "")).strip().rstrip(".")
    if not interest:
        return {
            "status": "OPEN", "title": "", "interest": "", "source": "no comprehension lane",
            "authorities": avoid + const,
            "text": "",
            "pull": ("the constitutional interest was NOT derived (no comprehension lane answered; "
                     + "; ".join(LLM_LOG[-2:]) + "). PULL: title the backstop from the units' own words — the grounded "
                     "constitutional authorities are on file ("
                     + "; ".join(f"{p.get('cite')} [corpus:{p.get('chunk')}] (U{p.get('unit')})" for p in const[:4])
                     + ") — a canned paragraph is not emitted in their place."),
        }
    rest = sorted({int(u) for u in (obj.get("units") or []) if str(u).strip().lstrip('-').isdigit()} & set(classes)) \
        or sorted(n for n, c in classes.items() if c.get("const_pins"))
    tail_av = "; ".join(f"{a['cite']} [corpus:{a['chunk']}]" for a in avoid)
    tail_const = "; ".join(f"{p.get('cite')} [corpus:{p.get('chunk')}]" for p in const)
    title = (f"If the Court does not dispose of this matter on the grounds above, the constitutional question — "
             f"{interest} — is squarely presented and cannot be avoided")
    text = (title + ". The canon that orders the grounds above is the Court's own: constitutional avoidance "
            "\"allows a court to dispose of an entire case on grounds that do not require the court to pass on a statute's "
            "constitutionality\", and \"[t]he Court will not pass upon a constitutional question although properly presented "
            "by the record, if there is also present some other ground upon which the case may be disposed of\" — "
            f"{tail_av}. That canon is a reason to reach the grounds above FIRST; it is not a reason to leave this one "
            f"unanswered if they are refused. The interest is pleaded on the authorities already of record in this filing: "
            f"{tail_const} (ground(s) {', '.join('U' + str(u) for u in rest)}).")
    return {"status": "PLEAD", "title": title, "interest": interest, "source": "comprehension on the units",
            "authorities": avoid + const, "text": text, "pull": "", "units": rest}


# ------------------------------------------------------------------ the ORDERING RULE
def order_grounds(units: list, classes: dict) -> list:
    """-> [{seq, unit, class, class_name, title, why}] sorted narrow -> broad, STABLE within a class (the
    units keep their own order), 'unknown' last among the pleadable classes, 'd' units folded into the
    backstop's own ground rather than pleaded as a lead."""
    rank = {"a": 0, "b": 1, "c": 2, "unknown": 3, "d": 4}
    seq = [(i, u) for i, u in enumerate(units)]
    seq.sort(key=lambda t: (rank.get(classes.get(t[1]["n"], {}).get("class", "unknown"), 3), t[0]))
    out = []
    for k, (_, u) in enumerate(seq, 1):
        c = classes.get(u["n"], {})
        cl = c.get("class", "unknown")
        out.append({"seq": k, "unit": u["n"], "class": cl, "class_name": CLASSES.get(cl, "unclassified"),
                    "title": u.get("title") or "", "why": c.get("why") or "", "src": c.get("source") or ""})
    return out


def check_ordering(ordered: list, bs: dict) -> dict:
    """The gate's own test, callable from m1_filing_gate: narrow->broad, and a backstop present and LAST."""
    rank = {"a": 0, "b": 1, "c": 2, "unknown": 3, "d": 4}
    seq = [rank.get(g["class"], 3) for g in ordered]
    monotone = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
    return {"monotone": monotone, "backstop_present": bool(bs), "backstop_last": True,
            "backstop_status": (bs or {}).get("status") or "MISSING",
            "classes": {k: sum(1 for g in ordered if g["class"] == k) for k in list(ORDER) + ["unknown"]},
            "ok": monotone and (bs or {}).get("status") == "PLEAD",
            "warn": ("" if monotone else "grounds are NOT narrow->broad; ")
                    + ("" if (bs or {}).get("status") == "PLEAD" else f"backstop {(bs or {}).get('status') or 'MISSING'} — "
                                                                      "constitutional ground not pleadable on the cells")}


# ------------------------------------------------------------------ the stub section (what the m1 gate reads)
HEADING = "## GROUNDS (ordering — constitutional avoidance: narrow -> broad, backstop LAST)"
BACKSTOP_LABEL = "BACKSTOP (class d — constitutional, LAST)"


def grounds_section(ordered: list, bs: dict, forum: str = "") -> list:
    """The ordering, VISIBLE in the stub / VEHICLES_*.md so the m1 gate can check it mechanically."""
    L = [HEADING, "",
         "- Rule of order: a court disposes of a case on the narrowest ground that ends it and reaches a constitutional "
         "question only if no other ground disposes of the matter — "
         + "; ".join(f"{a['cite']} [corpus:{a['chunk']}]" for a in AVOIDANCE_AUTHORITIES[:2]) + ".",
         ""]
    for g in ordered:
        L.append(f"{g['seq']}. [class {g['class']} — {g['class_name']}] U{g['unit']} — "
                 f"{re.sub(chr(10), ' ', g['title'])[:200]}")
        L.append(f"   - why this class: {g['why']} [{g['src']}]")
    L.append("")
    if (bs or {}).get("status") == "PLEAD":
        L += [f"{len(ordered)+1}. [{BACKSTOP_LABEL}] {bs['title']}",
              f"   - PLEAD, verbatim: {bs['text']}",
              f"   - derived by: {bs['source']}; rests on ground(s) {', '.join('U'+str(u) for u in bs.get('units') or [])}"]
    else:
        L += [f"{len(ordered)+1}. [{BACKSTOP_LABEL}] OPEN — not pleaded",
              f"   - PULL: {(bs or {}).get('pull') or 'no backstop computed'}"]
        for a in (bs or {}).get("authorities") or []:
            L.append(f"   - canon on file: {a['cite']} [corpus:{a['chunk']}]")
    chk = check_ordering(ordered, bs)
    L += ["", f"- ORDERING CHECK: monotone={chk['monotone']} backstop={chk['backstop_status']} "
              f"classes={chk['classes']}" + (f" WARN: {chk['warn']}" if chk["warn"] else "")]
    L += ["", "## CONSTRUCTION CANON (standing instruction for the drafter — apply where a rule is ambiguous, not everywhere)",
          "", CONSTRUCTION_CANON_INSTRUCTION, ""]
    return L


# ------------------------------------------------------------------ learnings (the connected write-back)
def write_learnings(ordered: list, bs: dict, case: str = "", out: Path | None = None) -> int:
    """Append the two patterns to output/learnings.jsonl (pipeline.connected_notes links them into the vault)."""
    p = Path(out) if out else (ROOT / "output" / "learnings.jsonl")
    chk = check_ordering(ordered, bs)
    recs = [
        {"text": "ORDERING CANON (class-level, every filing): emit grounds narrow -> broad — (a) forum procedural gates "
                 "(service, jurisdiction, pendency, standing/capacity), (b) statutory/regulatory compliance, (c) common "
                 "law/equity, (d) ALWAYS LAST a constitutional BACKSTOP ground ('If the Court does not dispose of this "
                 "matter on the grounds above, the constitutional question — <derived from the units> — is squarely "
                 "presented and cannot be avoided'). The canon is the court's own: constitutional avoidance "
                 "'allows a court to dispose of an entire case on grounds that do not require the court to pass on a "
                 "statute's constitutionality' [corpus:cap_us_557_193_northwest_austin_municipal_utility_distr#15d24e41ff] "
                 "(Ashwander v. TVA, 297 U.S. 288, 347 (1936) (Brandeis, J., concurring)); it 'allows and encourages the "
                 "court to first resolve procedural issues' [corpus:cap_us_529_473_slack_v_mcdaniel#71d0c2e40d]; in PA, "
                 "'a court should not reach the constitutional issue if the case can properly be decided on "
                 "non-constitutional grounds' [corpus:scotus_stilp_v_commonwealth_905a2d918#165032b384] (Ballou, 436 A.2d "
                 "186, 187 (1981)); 'a constitutional question must be reserved to the last' "
                 "[corpus:amjur2d_16_constitutional_law#1ccc816639]. Leading with the constitutional question hands the "
                 "court a reason to avoid the whole filing; leading with the gate makes the constitutional ground the one "
                 "it cannot avoid.",
         "tags": ["canon:avoidance", "pattern", "ordering"],
         "source": "pipeline/m2/ground_order.py (backlog child avoidance_order)"},
        {"text": "AVOIDANCE AS CONSTRUCTION (class-level): where a unit's rule text is genuinely ambiguous, plead the "
                 "reading that avoids the constitutional doubt as the one the court must adopt — 'a statute must be "
                 "construed, if fairly possible, so as to avoid doubts as to its constitutionality' "
                 "[corpus:amjur2d_16_constitutional_law#bedae2d5ed]; 'Federal statutes are to be so construed as to avoid "
                 "serious doubt of their constitutionality' "
                 "[corpus:cap_us_508_602_concrete_pipe_products_of_california_inc#bb063b0132] (Concrete Pipe, quoting "
                 "Machinists v. Street and Crowell v. Benson). Respect the limit on the same page: the canon 'will not be "
                 "pressed to the point of disingenuous evasion where the legislative intention is distinctly revealed' — "
                 "if the text is plain, plead the text and carry the constitutional question to the BACKSTOP ground. This "
                 "is a standing instruction in the composer prompt, applied where relevant, never everywhere.",
         "tags": ["canon:avoidance", "pattern", "construction"],
         "source": "pipeline/m2/ground_order.py (backlog child avoidance_order)"},
        {"text": f"ORDERING RUN: {len(ordered)} ground(s) ordered {chk['classes']}, backstop={chk['backstop_status']}"
                 + (f" ({bs.get('interest')})" if (bs or {}).get("interest") else "")
                 + (f"; WARN {chk['warn']}" if chk["warn"] else "") + (f"; case={case}" if case else ""),
         "tags": ["canon:avoidance", "ordering", "run"],
         "source": "pipeline/m2/ground_order.py"},
    ]
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(recs)


# ------------------------------------------------------------------ the one entry point the composer calls
def compose(units: list, rows: list, forum: str = "", case: str = "", llm=None,
            pins_of=None, grounded=None, learnings: Path | None = None) -> dict:
    """units: [{n, title}] (cells_to_stub.load_units); rows: the merged cells rows.
    pins_of / grounded: the host organ's own resolvers (cells_to_stub passes its own — one pin resolver, one
    GROUNDED test, never a second implementation here)."""
    if pins_of is None or grounded is None:
        _c2s = _sibling("cells_to_stub.py", "cells_to_stub_for_order")   # the host organ's own resolvers
        pins_of = pins_of or _c2s.pins_of
        grounded = grounded or _c2s._grounded
    by_unit: dict = {}
    for r in rows:
        if not grounded(r):
            continue
        p = pins_of(r)
        if not p:
            continue
        n = r.get("claim")
        try:
            n = int(n)
        except Exception:
            continue
        by_unit.setdefault(n, []).append({**r, "_pins": p})
    classes = classify_units(units, by_unit, llm=llm)
    bs = backstop_ground(units, by_unit, classes, forum=forum, llm=llm)
    ordered = order_grounds(units, classes)
    res = {"ordered": ordered, "backstop": bs, "classes": classes, "check": check_ordering(ordered, bs),
           "lines": grounds_section(ordered, bs, forum), "llm_log": list(LLM_LOG)}
    if learnings is not None:
        res["learnings"] = write_learnings(ordered, bs, case=case, out=learnings)
    return res


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="dry ordering report: cells -> narrow->broad grounds + backstop")
    ap.add_argument("--cells", nargs="+", required=True)
    ap.add_argument("--units", required=True)
    ap.add_argument("--forum", default=os.environ.get("COURT", ""))
    ap.add_argument("--case", default="")
    ap.add_argument("--llm", default="on", choices=["on", "off"])
    ap.add_argument("--json", dest="js", default=None)
    ap.add_argument("--learnings", default=None, help="append the pattern notes to THIS path (never the live file in a dry run)")
    a = ap.parse_args(argv)
    os.environ["AVOIDANCE_LLM"] = a.llm
    _c2s = _sibling("cells_to_stub.py", "cells_to_stub_for_order")
    rows, counts = _c2s.load_cells(a.cells)
    ud = _c2s.load_units(a.units)
    r = compose(ud["units"], rows, forum=a.forum, case=a.case, pins_of=_c2s.pins_of, grounded=_c2s._grounded,
                learnings=Path(a.learnings) if a.learnings else None)
    print("\n".join(r["lines"]))
    print("\n-- ordering check:", json.dumps(r["check"], ensure_ascii=False))
    for ln in r["llm_log"]:
        print("   [llm]", ln)
    if a.js:
        Path(a.js).write_text(json.dumps({k: v for k, v in r.items() if k != "classes"}, indent=1, ensure_ascii=False,
                                         default=str), encoding="utf-8")
        print("   json ->", a.js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
