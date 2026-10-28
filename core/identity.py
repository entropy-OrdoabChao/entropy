"""The person using the desk, and the placeholder renderer.

Every caption, signature block, return address and party match reads from here, never from a literal. Values come from
<home>/.env (written by Setup step 1 via pipeline.setup_env, or by hand) with environment variables as overrides.

Templates in the vault keep {{PLACEHOLDERS}} on disk, so they stay agnostic; the engine renders them at READ time:
  identity placeholders  {{USER_NAME}} {{USER_FIRST}} {{USER_LAST}} {{USER_ADDRESS}} {{USER_PHONE}} {{USER_EMAIL}} {{USER_SIGNATURE}}
                         {{STATE}} {{COUNTY}}                                   <- .env
  case placeholders      {{CASE_ID}} {{CASE_SLUG}} {{COURT_NAME}} {{JUDGE}} {{OPPOSING_PARTY}} {{CAPTION}}   <- the case's registry entry
  path placeholders      {{ENTROPY_HOME}} {{HOME}}
Unknown placeholders are left untouched so a later pass can fill them.
"""
import json, os, re
from pathlib import Path
from core.entropy_paths import entropy_root, package_root
_cache = None
KEYS = ("USER_NAME", "USER_FIRST", "USER_LAST", "USER_ADDRESS", "USER_PHONE", "USER_EMAIL", "USER_SIGNATURE", "STATE", "COUNTY")

def _load():
    global _cache
    if _cache is not None: return _cache
    d = {}
    try:
        for line in (entropy_root() / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception: pass
    for k in KEYS:
        if os.environ.get(k): d[k] = os.environ[k]
    name = d.get("USER_NAME", "").strip(); parts = name.split()
    d.setdefault("USER_FIRST", parts[0] if parts else ""); d.setdefault("USER_LAST", parts[-1] if len(parts) > 1 else (parts[0] if parts else ""))
    _cache = d; return d
def reload():
    global _cache; _cache = None
def get(key, default=""): return _load().get(key, default) or default
def name(): return get("USER_NAME", "PLAINTIFF")
def first(): return get("USER_FIRST")
def last(): return get("USER_LAST")
def caption_name(): return name().upper()
def address(): return get("USER_ADDRESS")
def phone(): return get("USER_PHONE")
def email(): return get("USER_EMAIL")
def signature():
    sig = get("USER_SIGNATURE").strip()
    return sig if sig and sig != "/s/" else ("/s/ " + name() if name() != "PLAINTIFF" else "/s/")
def state(): return get("STATE")
def county(): return get("COUNTY")
def party_key(): return re.sub(r"\W+", "_", name().lower()).strip("_")        # e.g. "maria_rivera": the registry's key for the person
def name_tokens(): return [t.lower() for t in re.findall(r"[A-Za-z]{3,}", name())]  # for "is this the plaintiff" checks
def is_me(text: str) -> bool:
    t = (text or "").lower(); toks = name_tokens()
    return bool(toks) and any(tok in t for tok in toks)

# ---------------------------------------------------------------- setup (writes .env; what Setup step 1 calls)
def write_env(values: dict) -> Path:
    """Merge USER_*/STATE/COUNTY values into <home>/.env (creates it if missing), keep every other line, reload."""
    p = entropy_root() / ".env"; p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else ["# Entropy settings (this computer only)."]
    vals = {k: str(v).strip() for k, v in values.items() if k in KEYS and v is not None}
    seen = set(); out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in vals: out.append(f"{k}={vals[k]}"); seen.add(k)
        else: out.append(line)
    for k, v in vals.items():
        if k not in seen: out.append(f"{k}={v}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8"); reload(); return p

# ---------------------------------------------------------------- rendering
def _case_values(case) -> dict:
    """case: a registry entry dict, a canonical id / slug, or None."""
    if isinstance(case, str):
        try:
            reg = json.loads((entropy_root() / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
            case = reg.get(case) or next((e for e in reg.values() if isinstance(e, dict) and e.get("slug") == case), None)
        except Exception: case = None
    if not isinstance(case, dict): return {}
    cid = case.get("canonical_id", ""); parties = [str(p) for p in (case.get("parties") or [])]
    opposing = next((p for p in parties if p != party_key() and not is_me(p)), "")
    return {"CASE_ID": cid.split(":", 1)[-1] if cid else "", "CASE_SLUG": case.get("slug", ""), "COURT_NAME": case.get("court_name") or case.get("case_court", ""),
            "JUDGE": case.get("judge", ""), "OPPOSING_PARTY": opposing.replace("_", " ").title(), "CAPTION": case.get("caption", "")}
def values(case=None) -> dict:
    v = {k: get(k) for k in KEYS}; v["USER_NAME"] = name(); v["USER_SIGNATURE"] = signature()
    v.update({"ENTROPY_HOME": str(entropy_root()), "HOME": str(Path.home())}); v.update(_case_values(case)); return v
def render(text: str, case=None) -> str:
    """Fill {{PLACEHOLDERS}} from .env and the case; unknown or empty ones are left as they are."""
    if not text or "{{" not in text: return text
    v = values(case)
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: v.get(m.group(1)) or m.group(0), text)
def _strip_frontmatter(t: str) -> str:
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) >= 3: return parts[2]
    return t
def block_text(rel: str, case=None) -> str:
    """A vault template block (e.g. 'boilerplate/signature_block_pro_se.md'), frontmatter stripped, placeholders rendered.
    Reads the person's vault first, then the package seed."""
    for base in (entropy_root() / "obsidian_sync" / "templates" / "blocks", package_root() / "vault_seed" / "templates" / "blocks"):
        p = base / rel
        if p.exists(): return render(_strip_frontmatter(p.read_text(encoding="utf-8")).strip(), case)
    return ""
def signature_block(case=None) -> str:
    """THE signature block: the vault stub rendered for this person. The desk shows this same text; the drafter appends it."""
    t = block_text("boilerplate/signature_block_pro_se.md", case)
    t = t.replace("[Plaintiff's address on file]", address() or "[address: set it in Setup]").replace("[Plaintiff's email on file]", email() or "").replace("[Plaintiff's phone on file]", phone() or "")
    return "\n".join(l for l in t.splitlines() if l.strip() or l == "")
