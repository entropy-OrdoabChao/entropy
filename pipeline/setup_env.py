"""setup_env — Setup step 1 ("About you"). Writes the person's details to <home>/.env and shows the rendered signature block.
Called by the desk's Setup page (through the API), by `entropy setup`, or by hand:
    python -m pipeline.setup_env --name "Maria Rivera" --address "1400 Walnut St, Philadelphia, PA 19102" --phone "215-555-0100" \
        --email maria@example.com --state PA --county Philadelphia [--signature "/s/ Maria Rivera"] [--json values.json]
Nothing else is touched: templates keep their {{PLACEHOLDERS}} on disk and are rendered when read (core.identity.render)."""
import argparse, json, sys
from pathlib import Path
from core import identity
def apply(values: dict) -> dict:
    p = identity.write_env(values)
    # the vault's memory.md "Who" section mirrors .env so a model booting from the vault knows the person
    try:
        from core.entropy_paths import entropy_root
        mem = entropy_root() / "obsidian_sync" / "memory.md"
        if mem.exists():
            t = mem.read_text(encoding="utf-8"); who = f"# Who\n\n{identity.name()}" + (f", {identity.address()}" if identity.address() else "") + (f", {identity.email()}" if identity.email() else "") + "\n"
            import re
            t2 = re.sub(r"# Who\n[\s\S]*?(?=\n# |\Z)", who.rstrip("\n") + "\n", t, count=1) if "# Who" in t else who + "\n" + t
            mem.write_text(t2, encoding="utf-8")
    except Exception: pass
    return {"env": str(p), "name": identity.name(), "signature_block": identity.signature_block()}
def main():
    ap = argparse.ArgumentParser(description="Setup step 1: who you are")
    ap.add_argument("--name"); ap.add_argument("--address"); ap.add_argument("--phone"); ap.add_argument("--email"); ap.add_argument("--state"); ap.add_argument("--county"); ap.add_argument("--signature")
    ap.add_argument("--json", help="a JSON file with USER_NAME, USER_ADDRESS, USER_PHONE, USER_EMAIL, STATE, COUNTY, USER_SIGNATURE")
    a = ap.parse_args()
    vals = json.loads(Path(a.json).read_text(encoding="utf-8")) if a.json else {}
    for k, v in (("USER_NAME", a.name), ("USER_ADDRESS", a.address), ("USER_PHONE", a.phone), ("USER_EMAIL", a.email), ("STATE", a.state), ("COUNTY", a.county), ("USER_SIGNATURE", a.signature)):
        if v is not None: vals[k] = v
    if not vals: ap.error("nothing to set")
    r = apply(vals); print(json.dumps({"env": r["env"], "name": r["name"]}, indent=1)); print("\nYour signature block (from the vault stub):\n" + r["signature_block"])
if __name__ == "__main__": main()
