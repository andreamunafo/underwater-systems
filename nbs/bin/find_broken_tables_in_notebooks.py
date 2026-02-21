#!/usr/bin/env python3
"""
Scan all notebooks under a directory for broken HTML <table> blocks.

Usage:
  python scan_broken_tables.py /path/to/notebooks_root
  # or, from repo root:
  python scan_broken_tables.py nbs
"""

import argparse
import json
import os
import re
from typing import List, Tuple, Optional


def cell_text(cell: dict) -> str:
    src = cell.get("source", "")
    if isinstance(src, list):
        return "".join(src)
    return src or ""


def first_clue_line(text: str, max_len: int = 180) -> str:
    for ln in text.splitlines():
        s = ln.strip()
        if s:
            return s[:max_len]
    return ""


def snippet_around(text: str, needle: str = "<table", radius: int = 220) -> str:
    low = text.lower()
    idx = low.find(needle)
    if idx == -1:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + radius)
    snip = text[start:end]
    return snip.replace("\n", "\\n")


def extract_table_blocks(text: str) -> List[str]:
    # capture well-formed <table ...> ... </table> blocks (non-greedy)
    return [m.group(0) for m in re.finditer(r"(<table\b[^>]*>)(.*?)(</table>)", text, flags=re.I | re.S)]


def tag_count(text: str, open_tag: str, close_tag: str) -> Tuple[int, int]:
    low = text.lower()
    return low.count(open_tag), low.count(close_tag)


def analyze_html_tables(text: str) -> List[str]:
    """
    Heuristic checks for HTML table malformation that commonly breaks myst/jupyter-book -> LaTeX.
    Returns list of issue strings. Empty => looks OK.
    """
    issues: List[str] = []
    low = text.lower()

    # Basic balance checks
    ot, ct = tag_count(text, "<table", "</table")
    if ot != ct:
        issues.append(f"unbalanced <table> tags (open {ot}, close {ct})")

    # td/th and tr balance checks (lightweight, not a full HTML parser)
    otr, ctr = tag_count(text, "<tr", "</tr")
    if otr != ctr:
        issues.append(f"unbalanced <tr> tags (open {otr}, close {ctr})")

    otd, ctd = tag_count(text, "<td", "</td")
    oth, cth = tag_count(text, "<th", "</th")
    if otd != ctd:
        issues.append(f"unbalanced <td> tags (open {otd}, close {ctd})")
    if oth != cth:
        issues.append(f"unbalanced <th> tags (open {oth}, close {cth})")

    # Table blocks parseability
    blocks = extract_table_blocks(text)
    if ot > 0 and not blocks:
        issues.append("found <table> but could not extract any <table>...</table> blocks (missing </table>?)")

    # Per-block structure checks
    for bi, blk in enumerate(blocks, start=1):
        if re.search(r"<tr\b", blk, flags=re.I) is None:
            issues.append(f"table#{bi} has no <tr>")

        # Does it have any row with a cell?
        trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", blk, flags=re.I | re.S)
        if trs:
            for ri, row in enumerate(trs, start=1):
                if re.search(r"<t[dh]\b", row, flags=re.I) is None:
                    issues.append(f"table#{bi} row#{ri} has no <td>/<th>")
        else:
            if re.search(r"<tr\b", blk, flags=re.I):
                issues.append(f"table#{bi} has <tr> but no </tr>")

        # Empty table content (can trigger “no columns” in some converters)
        inner = re.sub(r"(?is)^<table\b[^>]*>|</table>$", "", blk).strip()
        inner_no_tags = re.sub(r"(?is)<[^>]+>", "", inner).strip()
        if not inner_no_tags and re.search(r"(?is)<t[dh]\b", blk) is None:
            issues.append(f"table#{bi} looks empty / has no cells")

    return sorted(set(issues))


def scan_notebook(path: str) -> List[dict]:
    """
    Returns list of findings (one per broken cell).
    """
    findings: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return [{
            "notebook": path,
            "cell_index": None,
            "clue": "",
            "issues": [f"failed to read notebook JSON: {e}"],
            "snippet": ""
        }]

    for i, cell in enumerate(nb.get("cells", [])):
        txt = cell_text(cell)
        if "<table" not in txt.lower():
            continue

        issues = analyze_html_tables(txt)
        if issues:
            findings.append({
                "notebook": path,
                "cell_index": i,  # 0-based
                "clue": first_clue_line(txt),
                "issues": issues,
                "snippet": snippet_around(txt, "<table", 240),
            })

    return findings


def iter_notebooks(root: str) -> List[str]:
    out = []
    for r, _, files in os.walk(root):
        for fn in files:
            if fn.endswith(".ipynb") and not fn.endswith(".ipynb_checkpoints"):
                out.append(os.path.join(r, fn))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="Root directory to scan (default: .)")
    args = ap.parse_args()

    notebooks = iter_notebooks(args.root)
    if not notebooks:
        print(f"No notebooks found under: {os.path.abspath(args.root)}")
        raise SystemExit(1)

    all_findings = []
    for nb in notebooks:
        all_findings.extend(scan_notebook(nb))

    if not all_findings:
        print(f"✅ No broken <table> cells found in {len(notebooks)} notebooks.")
        return

    print(f"❌ Found {len(all_findings)} broken <table> cell(s) across {len(notebooks)} notebooks.\n")
    for f in all_findings:
        print("===")
        print(f"Notebook   : {f['notebook']}")
        print(f"Cell index : {f['cell_index']}  (0-based)")
        print(f"Clue line  : {f['clue']}")
        print("Issues:")
        for iss in f["issues"]:
            print(f"  - {iss}")
        if f["snippet"]:
            print("Snippet around <table>:")
            print(f"  {f['snippet']}")
        print()

    # Non-zero exit so you can use it in CI/scripts
    raise SystemExit(2)


if __name__ == "__main__":
    main()
