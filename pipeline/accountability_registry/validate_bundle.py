#!/usr/bin/env python3
"""
validate_bundle.py — pre-flight validator for accountability registry bundles.

Purpose: catch validator drift BEFORE merging additions yaml into registry.yaml.
Loads existing registry + additions into in-memory dict, runs schema.py validation,
reports ALL errors at once (not first-fail).

Usage:
  validate_bundle.py [additions.yaml]
  validate_bundle.py ~/beezle_work/bundle6/registry_additions_v0_5.yaml

Exit codes:
  0  validates clean
  1  validation errors (printed to stderr)
  2  setup error (file not found, yaml parse error, etc.)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import sys
import yaml
import copy
from pathlib import Path
from typing import Any, Dict, List

BEEZLE_ROOT = _eroot()
PKG_DIR = BEEZLE_ROOT / "pipeline" / "accountability_registry"

if str(BEEZLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BEEZLE_ROOT))


def _load_yaml(p: Path) -> Dict[str, Any]:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        print(f"✗ file not found: {p}", file=sys.stderr)
        sys.exit(2)
    except yaml.YAMLError as e:
        print(f"✗ yaml parse error in {p}: {e}", file=sys.stderr)
        sys.exit(2)


def _try_schema_import():
    try:
        from pipeline.accountability_registry import schema as _schema
        return _schema
    except Exception as e:
        print(f"✗ failed to import schema: {e}", file=sys.stderr)
        sys.exit(2)


def validate_merged(reg: Dict[str, Any], adds: Dict[str, Any]) -> List[str]:
    """Build merged registry in-memory and run schema validation. Returns list of error strings."""
    schema = _try_schema_import()
    errors: List[str] = []

    # Build merged copy
    merged = copy.deepcopy(reg)
    existing_forum_ids = {f.get("id") for f in merged.get("forums", []) or []}
    existing_bond_ids = {b.get("id") for b in merged.get("bonds", []) or []}
    existing_writ_ids = {w.get("id") for w in merged.get("writs", []) or []}

    # Collect what would be added
    for f in adds.get("forums_add", []) or []:
        if f.get("id") in existing_forum_ids:
            continue
        merged.setdefault("forums", []).append(f)
    for b in adds.get("bonds_add", []) or []:
        if b.get("id") in existing_bond_ids:
            continue
        merged.setdefault("bonds", []).append(b)
    for w in adds.get("writs_add", []) or []:
        if w.get("id") in existing_writ_ids:
            continue
        merged.setdefault("writs", []).append(w)

    # Dataclass construction (catches missing fields / extra fields)
    try:
        forums = []
        for f in merged.get("forums", []) or []:
            try:
                forums.append(schema.Forum(**f))
            except TypeError as e:
                errors.append(f"FORUM_DATACLASS({f.get('id', '?')}): {e}")
        bonds = []
        for b in merged.get("bonds", []) or []:
            try:
                bonds.append(schema.Bond(**b))
            except TypeError as e:
                errors.append(f"BOND_DATACLASS({b.get('id', '?')}): {e}")
        writs = []
        for w in merged.get("writs", []) or []:
            try:
                writs.append(schema.Writ(**w))
            except TypeError as e:
                errors.append(f"WRIT_DATACLASS({w.get('id', '?')}): {e}")
    except Exception as e:
        errors.append(f"DATACLASS_PHASE: {e}")
        return errors

    # Enum / scope / category checks per entry (collects ALL errors)
    for f in forums:
        try:
            schema.validate_forum(f)
        except schema.ValidationError as e:
            errors.append(f"FORUM_ENUM({f.id}): {e}")
        except Exception as e:
            errors.append(f"FORUM_OTHER({f.id}): {type(e).__name__}: {e}")
    for b in bonds:
        try:
            schema.validate_bond(b) if hasattr(schema, "validate_bond") else None
        except schema.ValidationError as e:
            errors.append(f"BOND_ENUM({b.id}): {e}")
        except Exception as e:
            errors.append(f"BOND_OTHER({b.id}): {type(e).__name__}: {e}")
    for w in writs:
        try:
            schema.validate_writ(w) if hasattr(schema, "validate_writ") else None
        except schema.ValidationError as e:
            errors.append(f"WRIT_ENUM({w.id}): {e}")
        except Exception as e:
            errors.append(f"WRIT_OTHER({w.id}): {type(e).__name__}: {e}")

    return errors


def diagnose_existing_drift(reg_path: Path) -> List[str]:
    """Scan existing registry for remedy values that look like free text rather than enums."""
    schema = _try_schema_import()
    reg = _load_yaml(reg_path)
    drift: List[str] = []
    allowed = getattr(schema, "REMEDY_TYPES", set())

    def _check(entry_id: str, field_name: str, values: List[str]):
        for v in values or []:
            if not isinstance(v, str):
                continue
            if v not in allowed:
                drift.append(f"{entry_id}.{field_name}='{v}' (not in REMEDY_TYPES)")

    for f in reg.get("forums", []) or []:
        _check(f.get("id", "?"), "remedies_available", f.get("remedies_available", []))
        _check(f.get("id", "?"), "remedies_NOT_available", f.get("remedies_NOT_available", []))

    for w in reg.get("writs", []) or []:
        _check(w.get("id", "?"), "remedies_available", w.get("remedies_available", []))

    return drift


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("additions_yaml", nargs="?", default=None,
                    help="Path to a bundle's registry_additions_*.yaml")
    ap.add_argument("--registry", default=str(PKG_DIR / "registry.yaml"),
                    help="Path to existing registry.yaml")
    ap.add_argument("--diagnose-only", action="store_true",
                    help="Scan existing registry for drift; don't merge anything")
    args = ap.parse_args()

    reg_path = Path(args.registry)
    if not reg_path.exists():
        print(f"✗ registry not found: {reg_path}", file=sys.stderr)
        sys.exit(2)

    print("═══════════════════════════════════════════════════════════════════════")
    print("  PRE-FLIGHT VALIDATOR — accountability registry")
    print("═══════════════════════════════════════════════════════════════════════")

    # 1. Diagnose existing
    print("\n── Step 1: diagnose existing registry.yaml for drift ──")
    drift = diagnose_existing_drift(reg_path)
    if drift:
        print(f"  ✗ {len(drift)} drift entry(ies) in existing registry:")
        for d in drift:
            print(f"    · {d}")
    else:
        print("  ✓ existing registry has no remedy-enum drift")

    if args.diagnose_only:
        sys.exit(1 if drift else 0)

    # 2. Validate additions if provided
    if args.additions_yaml:
        print(f"\n── Step 2: validate merge of {args.additions_yaml} ──")
        add_path = Path(args.additions_yaml)
        if not add_path.exists():
            print(f"  ✗ additions file not found: {add_path}", file=sys.stderr)
            sys.exit(2)

        reg = _load_yaml(reg_path)
        adds = _load_yaml(add_path)
        errors = validate_merged(reg, adds)

        if errors:
            print(f"  ✗ {len(errors)} validation error(s) would block merge:")
            for e in errors:
                print(f"    · {e}")
            print("\n  → fix yaml or extend schema enums BEFORE running install.")
            sys.exit(1)
        else:
            n_f = len(adds.get("forums_add", []) or [])
            n_b = len(adds.get("bonds_add", []) or [])
            n_w = len(adds.get("writs_add", []) or [])
            print(f"  ✓ additions validate clean: +{n_f} forum(s), +{n_b} bond(s), +{n_w} writ(s)")
            print(f"  ✓ safe to run install script")

    # 3. Final
    print("\n═══════════════════════════════════════════════════════════════════════")
    if drift:
        print("  PRE-FLIGHT: existing drift detected (additions OK, but registry needs hotfix)")
        sys.exit(1)
    print("  PRE-FLIGHT: clean")
    print("═══════════════════════════════════════════════════════════════════════")
    sys.exit(0)


if __name__ == "__main__":
    main()
