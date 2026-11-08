#!/usr/bin/env python3
"""Locale gate for the web / Flutter bundles (docs/LIGHT_MODE_I18N_PLAN.md 2.2).

Checks:
  1. mirror   mobile/assets/locales/<lang> is byte-identical to the web dir
              (web-only namespaces excluded). vi-VN may be absent on both sides.
  2. keys     zh-CN key sets equal en-US per namespace; vi-VN may omit files/keys
              but must not add keys.
  3. zh purity  no Latin words in zh-CN strings (Light-reachable namespaces).
  4. en purity  no CJK in en-US strings (same scope).

Default mode: mirror + keys fail with exit 1; purity is reported only.
--strict-purity: purity violations fail too.  --report: print violations only, exit 0.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "frontend-v2/src/locales"
MOBILE = ROOT / "mobile/assets/locales"
MOBILE_ONLY = ROOT / "mobile/assets/locales-mobile"
I18N_DART = ROOT / "mobile/lib/shared/i18n/i18n.dart"

WEB_ONLY = {"admin-trajectories", "admin-pricing", "companion"}
BASE, ZH, VI = "en-US", "zh-CN", "vi-VN"
OPTIONAL_LANGS = {VI}
# Pre-existing web->mobile drift on light-mode (found when this gate was added).
# Reported as warnings, not failures; remove entries when mobile is re-synced.
KNOWN_MIRROR_DRIFT = {"admin-examples", "admin", "knowledge", "settings"}
PLURAL_RE = re.compile(r"_(zero|one|two|few|many|other)$")
PURITY_NS = ("light first_video onboarding common errors auth auth-center chat "
             "voice billing inbox settings legal memory resources workspace").split()
LATIN_ALLOW = {"bossip", "BossIP", "BOSSIP"}
VAR_RE = re.compile(r"\{\{.*?\}\}")
LATIN_RE = re.compile(r"[A-Za-z]{2,}")
CJK_RE = re.compile("[一-鿿]")


def langs(base):
    return {p.name for p in base.iterdir() if p.is_dir()} if base.is_dir() else set()


def files(d):
    return {p.name: p for p in d.glob("*.json") if p.stem not in WEB_ONLY} if d.is_dir() else {}


def flatten(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from flatten(v, f"{prefix}[{i}]")
    elif isinstance(node, str):
        yield prefix, node


def load(path):
    # i18next plural suffixes differ per language (zh has only _other): compare the stem
    return {PLURAL_RE.sub("", k): v for k, v in flatten(json.loads(path.read_text(encoding="utf-8")))}


def check_mirror(errs, warns):
    web_langs, mob_langs = langs(WEB), langs(MOBILE)
    for lang in sorted(web_langs | mob_langs):
        w, m = WEB / lang, MOBILE / lang
        if lang not in web_langs:
            errs.append(f"mirror: {lang} exists in mobile but not in web")
            continue
        if lang not in mob_langs:
            if lang not in OPTIONAL_LANGS:
                errs.append(f"mirror: {lang} missing from mobile/assets/locales")
            continue
        wf, mf = files(w), files(m)
        for name in sorted(set(wf) | set(mf)):
            out = warns if name[:-5] in KNOWN_MIRROR_DRIFT else errs
            if name not in mf:
                out.append(f"mirror: {lang}/{name} missing in mobile")
            elif name not in wf:
                out.append(f"mirror: {lang}/{name} missing in web")
            elif wf[name].read_bytes() != mf[name].read_bytes():
                out.append(f"mirror: {lang}/{name} differs between web and mobile")
    if I18N_DART.is_file():
        text = I18N_DART.read_text(encoding="utf-8")
        for name in sorted(files(WEB / BASE)):
            ns = name[:-5]
            if not re.search(rf"^\s*'{re.escape(ns)}',\s*$", text, re.M):
                (warns if ns in KNOWN_MIRROR_DRIFT else errs).append(
                    f"mirror: namespace '{ns}' is not registered in i18n.dart")


def ns_files(lang):
    """namespace -> path, over web (all) and app-only locales."""
    out = {p.stem: p for p in files(WEB / lang).values()}
    for p in (MOBILE_ONLY / lang).glob("*.json") if (MOBILE_ONLY / lang).is_dir() else []:
        out[p.stem] = p
    return out


def check_keys(errs):
    base = ns_files(BASE)
    zh, vi = ns_files(ZH), ns_files(VI)
    for ns, bp in sorted(base.items()):
        bk = set(load(bp))
        if ns not in zh:
            errs.append(f"keys: {ZH}/{ns} missing")
        else:
            zk = set(load(zh[ns]))
            for k in sorted(bk - zk):
                errs.append(f"keys: {ZH}/{ns} missing key {k}")
            for k in sorted(zk - bk):
                errs.append(f"keys: {ZH}/{ns} extra key {k}")
        if ns in vi:
            for k in sorted(set(load(vi[ns])) - bk):
                errs.append(f"keys: {VI}/{ns} extra key {k}")
    for ns in sorted(set(zh) - set(base)):
        errs.append(f"keys: {ZH}/{ns} has no {BASE} counterpart")
    for ns in sorted(set(vi) - set(base)):
        errs.append(f"keys: {VI}/{ns} has no {BASE} counterpart")


def check_purity():
    viol = defaultdict(list)  # ns -> [(kind, file, key, text)]
    for lang, kind, rx in ((ZH, "zh-latin", LATIN_RE), (BASE, "en-cjk", CJK_RE)):
        nsf = ns_files(lang)
        for ns in PURITY_NS:
            if ns not in nsf:
                continue
            rel = nsf[ns].relative_to(ROOT)
            for k, v in load(nsf[ns]).items():
                s = VAR_RE.sub("", v)
                if kind == "zh-latin":
                    bad = [w for w in rx.findall(s) if w not in LATIN_ALLOW]
                    hit = bool(bad)
                else:
                    hit = bool(rx.search(s))
                if hit:
                    viol[ns].append((kind, str(rel), k, v))
    return viol


def print_purity(viol):
    counts = {"zh-latin": 0, "en-cjk": 0}
    print("== purity violations (by namespace) ==")
    for ns in sorted(viol):
        items = viol[ns]
        z = sum(1 for i in items if i[0] == "zh-latin")
        print(f"\n[{ns}] zh-latin={z} en-cjk={len(items) - z}")
        for kind, f, k, v in items:
            counts[kind] += 1
            print(f"  {kind:8} {f} :: {k} :: {v[:60].replace(chr(10), ' ')}")
    print(f"\nTOTAL zh-latin={counts['zh-latin']} en-cjk={counts['en-cjk']}")
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="print purity violations only, exit 0")
    ap.add_argument("--strict-purity", action="store_true", help="purity violations fail the run")
    a = ap.parse_args()

    viol = check_purity()
    if a.report:
        print_purity(viol)
        return 0

    errs, warns = [], []
    check_mirror(errs, warns)
    check_keys(errs)
    counts = print_purity(viol) if viol else {"zh-latin": 0, "en-cjk": 0}
    total = sum(counts.values())
    for w in warns:
        print("WARN (known drift):", w, file=sys.stderr)
    for e in errs:
        print("FAIL:", e, file=sys.stderr)
    if errs:
        return 1
    if total and a.strict_purity:
        print(f"FAIL: {total} purity violations", file=sys.stderr)
        return 1
    note = f" ({total} purity violations reported, not enforced)" if total else ""
    print("OK: locales mirror, key alignment" + (", purity" if a.strict_purity else "") + " pass" + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
