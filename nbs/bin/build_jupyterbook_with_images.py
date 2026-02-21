#!/usr/bin/env python3
"""
Build Jupyter Book PDF reliably (nbdev-style repo) by:

- (optional) copying images to nbs/_build/html/images (handy for HTML preview)
- rsyncing the WHOLE repo to a temp path with no spaces
- in TEMP ONLY:
    - create a PDF-safe copy of notebooks (e.g., remove/replace videos)
    - patch leftover HTML patterns like <img src="...mp4">
    - patch markdown patterns that commonly break LaTeX (e.g., leading '\\\\', quoted image paths)
    - optionally prepend page breaks
    - patch TEMP myst.yml to point from nbs/ -> nbs_pdf/
- run `jupyter book build <nbs> --pdf` from the TEMP project root
- verify LaTeX build succeeded (no latexmk failure / LaTeX Error)
- copy the resulting PDF back into the REAL repo at: nbs/_build/exports/<output>
- also copy logs back when failing (so you can debug fast)

Assumes nbdev layout:

  project_root/
    nbs/
      myst.yml
      *.ipynb
      bin/
        build_jupyterbook_with_images.py   (this script)
        create_pdf_safe_notebooks.py
        prepend_page_break.py

Run from the project root folder:

    python nbs/bin/build_jupyterbook_with_images.py . --strict --debug --output underwater-systems.pdf --keep-temp

  (remove --keep-temp to automatically delete the tmp folder)
  
IMPORTANT:
- This script should never modify your real repo (only temp copy), except copying the final PDF + logs back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# -----------------------------------------------------------------------------
# Imports from sibling scripts in nbs/bin
# -----------------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

try:
    from create_pdf_safe_notebooks import create_pdf_safe_version
except Exception as e:
    raise RuntimeError(f"Could not import create_pdf_safe_notebooks.py from {THIS_DIR}: {e}")

try:
    from prepend_page_break import process_all_notebooks as prepend_page_breaks_in_dir
except Exception as e:
    raise RuntimeError(f"Could not import prepend_page_break.py from {THIS_DIR}: {e}")


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".avi")


def run(cmd: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    print("\n> " + " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,              # IMPORTANT: Myst sometimes returns 0 even when LaTeX fails
        text=True,
        stdout=sys.stdout,        # stream output live
        stderr=sys.stderr,
    )


def copy_images_for_html(nbs_root: Path) -> None:
    """
    Optional convenience: copy nbs/images -> nbs/_build/html/images.
    """
    src = nbs_root / "images"
    dst = nbs_root / "_build" / "html" / "images"

    if not src.exists():
        print(f"[warn] No images folder found at: {src} (skipping copy)")
        return

    dst.mkdir(parents=True, exist_ok=True)

    print("\nImages will be copied from")
    print(f" SOURCE: '{src}'")
    print(f" DESTINATION: '{dst}'")
    ans = input("Do you want to proceed? (yes/no): ").strip().lower()
    if ans not in ("y", "yes"):
        print("Skipping image copy.")
        return

    for root, _, files in os.walk(src):
        rootp = Path(root)
        rel = rootp.relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for fn in files:
            s = rootp / fn
            d = dst / rel / fn
            shutil.copy2(s, d)

    print("Images copied successfully.")


def rsync_to_temp(project_root: Path, temp_project_root: Path) -> None:
    """
    Mirror project_root into temp_project_root using rsync.
    """
    temp_project_root.parent.mkdir(parents=True, exist_ok=True)

    excludes = [
        ".git",
        ".DS_Store",
        "**/.ipynb_checkpoints",
        "**/__pycache__",
        "nbs/_build",
        "_build",
        ".venv",
        "venv",
    ]

    cmd = ["rsync", "-a", "--delete"]
    for ex in excludes:
        cmd += ["--exclude", ex]

    cmd += [str(project_root) + "/", str(temp_project_root)]

    print("\nRsyncing project to temp path (no spaces):")
    print(f"  SRC : {project_root}")
    print(f"  DEST: {temp_project_root}")
    run(cmd)

    if not temp_project_root.exists():
        raise RuntimeError("rsync failed: temp_project_root does not exist after rsync")

    tpl = temp_project_root / "_templates" / "tex" / "underwater_systems_latex" / "template.tex"
    if not tpl.exists():
        raise RuntimeError(f"Local template missing in TEMP: {tpl}")
    print(f"[ok] Local template exists in TEMP: {tpl}")


# --- Markdown image attribute blocks:  ![alt](path){...}  and  ![alt][ref]{...}
MD_IMG_ATTR_RE = re.compile(
    r'(!\[[^\]]*\]\(\s*[^)]*?\s*\))\s*(?:\{[^{}]*\}\s*)+'
)
MD_REF_IMG_ATTR_RE = re.compile(
    r'(!\[[^\]]*\]\[[^\]]+\])\s*(?:\{[^{}]*\}\s*)+'
)

# --- HTML <img ...> attribute removal (quoted OR unquoted)
HTML_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', flags=re.IGNORECASE)
HTML_ATTR_REMOVE_RE = re.compile(
    r'\s+(?:width|height|style|longdesc)\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
    flags=re.IGNORECASE
)

def patch_markdown_and_html_image_attrs_inplace(nb_path: Path) -> bool:
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        src = cell.get("source", [])
        is_str = isinstance(src, str)
        text = src if is_str else "".join(src)

        # 1) Strip quarto/pandoc attribute blocks after markdown images
        new_text = MD_IMG_ATTR_RE.sub(r"\1", text)
        new_text2 = MD_REF_IMG_ATTR_RE.sub(r"\1", new_text)

        # 2) Strip width/height/style/longdesc attributes from HTML <img ...>
        def _clean_img_tag(m: re.Match) -> str:
            tag = m.group(0)
            return HTML_ATTR_REMOVE_RE.sub("", tag)

        new_text3 = HTML_IMG_TAG_RE.sub(_clean_img_tag, new_text2)

        if new_text3 != text:
            changed = True
            # write back preserving original type
            cell["source"] = new_text3 if is_str else new_text3.splitlines(keepends=True)

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def convert_quarto_figures_to_myst_inplace_v0(nb_path: Path) -> bool:
    """
    Convert Quarto/Pandoc-style images like:
      ![Figure: Caption here](./img.png){width=90%}
    into a MyST figure directive (caption will work in LaTeX/PDF):
      ```{figure} ./img.png
      :align: center
      :width: 90%

      Caption here
      ```
    Only converts when alt text starts with 'Figure:' or 'Fig:' (case-insensitive).
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    # Match markdown image + one-or-more attribute blocks
    # Captures: alt, path, attrs (e.g. "{width=90%}")
    IMG_RE = re.compile(
        r'!\[(?P<alt>[^\]]*)\]\(\s*(?P<path>[^)]+?)\s*\)\s*(?P<attrs>(?:\{[^{}]*\}\s*)+)',
        flags=re.IGNORECASE,
    )

    WIDTH_RE = re.compile(
        r'width\s*=\s*(?P<w>\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:px|pt|cm|mm|in))',
        flags=re.IGNORECASE,
    )

    FIG_PREFIX_RE = re.compile(r'^\s*(?:figure|fig)\s*:\s*', flags=re.IGNORECASE)

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        orig_src = cell.get("source", [])
        is_str = isinstance(orig_src, str)
        text = orig_src if is_str else "".join(orig_src)

        def _repl(m: re.Match) -> str:
            alt = (m.group("alt") or "").strip()
            if not FIG_PREFIX_RE.match(alt):
                return m.group(0)  # leave unchanged

            caption = FIG_PREFIX_RE.sub("", alt).strip() or alt
            path = (m.group("path") or "").strip()
            attrs = m.group("attrs") or ""

            width = None
            mw = WIDTH_RE.search(attrs)
            if mw:
                width = mw.group("w")

            opts = [":align: center"]
            if width:
                opts.append(f":width: {width}")

            # Ensure clean separation from surrounding text
            return (
                f"\n```{{figure}} {path}\n"
                + "\n".join(opts)
                + f"\n\n{caption}\n```\n"
            )

        new_text = IMG_RE.sub(_repl, text)
        if new_text != text:
            changed = True
            cell["source"] = new_text if is_str else new_text.splitlines(keepends=True)

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def convert_quarto_figures_to_myst_inplace(nb_path: Path) -> bool:
    """
    Convert Quarto/Pandoc-style images like:
      ![Figure: Caption](./img.png){width=90%}
    into MyST figure directive with a real caption and LaTeX-safe width.

    width=90% -> :width: 0.9\\linewidth
    width=10cm/200px/etc -> kept as-is.
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    IMG_RE = re.compile(
        r'!\[(?P<alt>[^\]]*)\]\(\s*(?P<path>[^)]+?)\s*\)\s*(?P<attrs>(?:\{[^{}]*\}\s*)+)',
        flags=re.IGNORECASE,
    )
    WIDTH_RE = re.compile(
        r'width\s*=\s*(?P<w>\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:px|pt|cm|mm|in))',
        flags=re.IGNORECASE,
    )
    FIG_PREFIX_RE = re.compile(r'^\s*(?:figure|fig)\s*:\s*', flags=re.IGNORECASE)

    def _latex_width(w: str) -> str:
        w = w.strip()
        if w.endswith("%"):
            try:
                p = float(w[:-1]) / 100.0
                # clamp a bit just in case
                p = max(0.01, min(1.0, p))
                return f"{p:.3g}\\linewidth"
            except Exception:
                return w  # fallback
        return w

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        orig_src = cell.get("source", [])
        is_str = isinstance(orig_src, str)
        text = orig_src if is_str else "".join(orig_src)

        def _repl(m: re.Match) -> str:
            alt = (m.group("alt") or "").strip()
            if not FIG_PREFIX_RE.match(alt):
                return m.group(0)

            caption = FIG_PREFIX_RE.sub("", alt).strip() or alt
            path = (m.group("path") or "").strip()
            attrs = m.group("attrs") or ""

            width = None
            mw = WIDTH_RE.search(attrs)
            if mw:
                width = _latex_width(mw.group("w"))

            opts = [":align: center"]
            if width:
                opts.append(f":width: {width}")

            return (
                f"\n```{{figure}} {path}\n"
                + "\n".join(opts)
                + f"\n\n{caption}\n```\n"
            )

        new_text = IMG_RE.sub(_repl, text)
        if new_text != text:
            changed = True
            cell["source"] = new_text if is_str else new_text.splitlines(keepends=True)

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed

def patch_html_img_video_inplace_v0(nb_path: Path) -> bool:
    """
    Replace HTML <img src="...mp4"> (or mov/webm/...) with a text note.
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    img_video_re = re.compile(
        r"""<img\b[^>]*\bsrc=["']([^"']+\.(?:mp4|mov|webm|m4v|avi))["'][^>]*>""",
        flags=re.IGNORECASE,
    )

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        src = cell.get("source", [])
        src_lines = [src] if isinstance(src, str) else src

        new_lines: list[str] = []
        for line in src_lines:
            m = img_video_re.search(line)
            if m:
                vid = m.group(1)
                new_lines.append(
                    f"> **Video omitted in PDF build**: `{vid}` (available in the online version)\n"
                )
                changed = True
            else:
                new_lines.append(line)

        cell["source"] = new_lines

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed

def patch_html_img_video_inplace(nb_path: Path) -> bool:
    """
    Replace HTML <img src="...mp4"> (or mov/webm/...) with a text note.

    - Works on markdown cells only
    - Preserves the original type of cell["source"] (list[str] vs str)
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    img_video_re = re.compile(
        r"""<img\b[^>]*\bsrc=["']([^"']+\.(?:mp4|mov|webm|m4v|avi))["'][^>]*>""",
        flags=re.IGNORECASE,
    )

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        orig_src = cell.get("source", [])
        is_str = isinstance(orig_src, str)
        src_lines = [orig_src] if is_str else list(orig_src)

        new_lines: list[str] = []
        for line in src_lines:
            m = img_video_re.search(line)
            if m:
                vid = m.group(1)
                new_lines.append(
                    f"> **Video omitted in PDF build**: `{vid}` (available in the online version)\n"
                )
                changed = True
            else:
                new_lines.append(line)

        cell["source"] = "".join(new_lines) if is_str else new_lines

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed

def patch_markdown_for_latex_inplace_v0(nb_path: Path) -> bool:
    """
    Patch markdown patterns that commonly break LaTeX export:
      - Leading '\\\\' at start of a line  -> LaTeX "There's no line here to end."
      - Image links with quotes/empty/space: ![]('path') or ![]( 'path' ) etc.
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    # 1) leading \\ at start of a line -> replace with blank line (or <br>)
    #    This is a big source of "There's no line here to end."
    leading_double_slash = re.compile(r"^\s*\\\\\s*$")

    # 2) quoted image paths: ![alt]('path') or ![alt]("path") -> ![alt](path)
    quoted_img = re.compile(r"!\[([^\]]*)\]\(\s*['\"]([^'\"]+)['\"]\s*\)")

    # 3) image link with empty/garbage target can become: File `' not found.
    #    e.g. ![]('') or ![](' ') -> replace with a note.
    empty_img = re.compile(r"!\[([^\]]*)\]\(\s*['\"]?\s*['\"]?\s*\)")

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        src = cell.get("source", [])
        lines = [src] if isinstance(src, str) else list(src)

        out: list[str] = []
        for line in lines:
            orig = line

            # remove pure leading '\\' lines
            if leading_double_slash.match(line):
                line = "\n"
                changed = True

            # fix quoted image paths
            line2 = quoted_img.sub(r"![\1](\2)", line)
            if line2 != line:
                line = line2
                changed = True

            # replace empty image targets with a harmless note
            line3 = empty_img.sub(r"> **Image omitted (empty reference)**\n", line)
            if line3 != line:
                line = line3
                changed = True

            out.append(line)

        cell["source"] = out

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed

def patch_markdown_for_latex_inplace(nb_path: Path) -> bool:
    """
    Patch markdown patterns that commonly break LaTeX export (markdown cells only):

      1) Lines that are just '\\\\' (optionally surrounded by whitespace)
         -> replace with a blank line (avoids: "There's no line here to end.")

      2) Quoted image paths:
           ![alt]('path')  or  ![alt]("path")
         -> ![alt](path)

      3) Empty/garbage image targets:
           ![]('')  ![]("")  ![]()  ![]('   ')
         -> replace with a harmless note.
    """
    changed = False
    data = json.loads(nb_path.read_text(encoding="utf-8"))

    leading_double_slash = re.compile(r"^\s*\\\\\s*$")
    quoted_img = re.compile(r"!\[([^\]]*)\]\(\s*['\"]([^'\"]+)['\"]\s*\)")
    empty_img = re.compile(r"!\[([^\]]*)\]\(\s*['\"]?\s*['\"]?\s*\)")

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "markdown":
            continue

        orig_src = cell.get("source", [])
        is_str = isinstance(orig_src, str)
        lines = [orig_src] if is_str else list(orig_src)

        out: list[str] = []
        for line in lines:
            # 1) remove pure leading '\\' lines
            if leading_double_slash.match(line):
                line = "\n"
                changed = True

            # 2) fix quoted image paths
            line2 = quoted_img.sub(r"![\1](\2)", line)
            if line2 != line:
                line = line2
                changed = True

            # 3) replace empty image targets with a harmless note
            line3 = empty_img.sub(r"> **Image omitted (empty reference)**\n", line)
            if line3 != line:
                line = line3
                changed = True

            out.append(line)

        cell["source"] = "".join(out) if is_str else out

    if changed:
        nb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def patch_all_notebooks(nbs_root: Path) -> None:
    """
    Apply patches across all notebooks in the given dir.
    """
    touched_video = 0
    touched_md = 0
    touched_md_images = 0

    for nb in nbs_root.rglob("*.ipynb"):
        s = str(nb)
        if ".ipynb_checkpoints" in s or "/_build/" in s or "\\_build\\" in s:
            continue

        if convert_quarto_figures_to_myst_inplace(nb):
            touched_md_images += 1
        if patch_markdown_and_html_image_attrs_inplace(nb):
            touched_md_images += 1
        if patch_html_img_video_inplace(nb):
            touched_video += 1
        if patch_markdown_for_latex_inplace(nb):
            touched_md += 1

    if touched_video:
        print(f"[ok] Patched {touched_video} notebook(s) containing <img src='...video'> HTML.")
    else:
        print("[ok] No <img src='...video'> HTML patterns found.")

    if touched_md:
        print(f"[ok] Patched {touched_md} notebook(s) for LaTeX-hostile markdown.")
    else:
        print("[ok] No LaTeX-hostile markdown patterns found (best-effort).")

    if touched_md_images:
        print(f"[ok] Patched {touched_md_images} notebook(s) for Quarto image attributes.")
    else:
        print("[ok] No Quarto image attribute patterns found (best-effort).")


def patch_temp_myst_yml(temp_project_root: Path) -> Path:
    """
    In TEMP ONLY:
    patch nbs/myst.yml so all references to 'nbs/<something>' become 'nbs_pdf/<something>'
    """
    myst_path = temp_project_root / "myst.yml"  
    if not myst_path.exists():
        raise FileNotFoundError(f"Cannot find myst.yml at: {myst_path}")

    txt = myst_path.read_text(encoding="utf-8")
    patched = txt.replace("nbs/", "nbs_pdf/")

    if patched == txt:
        print("[warn] myst.yml patch made no changes (did you already patch it?)")
    else:
        myst_path.write_text(patched, encoding="utf-8")
        print(f"[ok] Patched TEMP myst.yml: {myst_path} (nbs/ -> nbs_pdf/)")

    return myst_path


def find_latest_myst_build_folder(temp_project_root: Path) -> Optional[Path]:
    """
    Myst writes logs into _build/temp/mystXXXXXX
    """
    temp_dir = temp_project_root / "_build" / "temp"
    if not temp_dir.exists():
        return None
    myst_dirs = [p for p in temp_dir.iterdir() if p.is_dir() and p.name.startswith("myst")]
    if not myst_dirs:
        return None
    return max(myst_dirs, key=lambda p: p.stat().st_mtime)


def latex_logs_have_errors(myst_build_dir: Path) -> bool:
    """
    Detect LaTeX failures robustly by scanning index.shell.log / index.log.
    """
    candidates = [
        myst_build_dir / "index.shell.log",
        myst_build_dir / "index.log",
    ]
    text = ""
    for p in candidates:
        if p.exists():
            text += "\n" + p.read_text(errors="ignore")

    if not text.strip():
        return False

    needles = [
        "LaTeX Error:",
        "Command failed:",
        "latexmk",
        "Fatal error occurred",
        "Emergency stop",
    ]
    return any(n in text for n in needles)


def is_pdf_valid(pdf_path: Path) -> bool:
    """
    Quick sanity check: starts with %PDF and ends with %%EOF within last ~2KB.
    """
    if not pdf_path.exists() or pdf_path.stat().st_size < 1024:
        return False
    with pdf_path.open("rb") as f:
        head = f.read(8)
        f.seek(max(pdf_path.stat().st_size - 2048, 0))
        tail = f.read()
    return head.startswith(b"%PDF") and (b"%%EOF" in tail)


def build_pdf_in_temp(
    temp_project_root: Path,
    strict: bool,
    debug: bool,
    output: str,
    template: str,
) -> Path:
    """
    Build from TEMP using the patched myst.yml in temp/nbs, but references nbs_pdf inside it.
    So we call:
        jupyter book build nbs --pdf
    """
    cmd = ["jupyter", "book", "build", "nbs", "--pdf"]
    if strict:
        cmd += ["--strict"]
    if debug:
        cmd += ["--debug"]
    if output:
        cmd += ["--output", output]
    if template:
        cmd += ["--template", template]

    print("\nBuilding PDF in temp folder:")
    print(f"  CWD: {temp_project_root}")
    run(cmd, cwd=temp_project_root)

    ########
    # # after running `jupyter book build ...`
    # temp_root = Path(temp_project_root)  # or whatever your var is called

    # # 1) First, try MyST's explicit output filename (what we passed with --output)
    # if output_filename:
    #     myst_pdf = temp_root / output_filename
    # else:
    #     # fallback: guess a reasonable default name
    #     myst_pdf = temp_root / "underwater-systems.pdf"

    # candidate_paths = []

    # if myst_pdf.exists():
    #     candidate_paths.append(myst_pdf)

    # # 2) Fallbacks: old index.pdf-based guesses
    # candidate_paths.append(temp_root / "underwater-systems" / "index.pdf")
    # candidate_paths.extend(temp_root.glob("_build/temp/myst*/index.pdf"))

    # pdf_path = None
    # for p in candidate_paths:
    #     if p.exists() and p.is_file():
    #         pdf_path = p
    #         break

    # if pdf_path is None:
    #     tried = "\n  - ".join(str(p) for p in candidate_paths)
    #     raise RuntimeError(
    #         "PDF not found or invalid after build.\n"
    #         f"Tried:\n  - {tried}"
    #     )
    ####################

    # Prefer the "exported" PDF, not the temp one
    # With your myst.yml output: underwater-systems -> should create underwater-systems/index.pdf
    print('>>>>>', output)
    # export_pdf = temp_project_root / "underwater-systems" / output
    export_pdf = temp_project_root / output

    myst_dir = find_latest_myst_build_folder(temp_project_root)
    temp_pdf = myst_dir / "index.pdf" if myst_dir else None

    # If LaTeX failed, DO NOT accept any PDF as "ok"
    if myst_dir and latex_logs_have_errors(myst_dir):
        # keep the most useful logs in a stable place
        raise RuntimeError(
            "LaTeX reported errors during PDF build.\n"
            f"Check logs:\n"
            f"  - {myst_dir / 'index.shell.log'}\n"
            f"  - {myst_dir / 'index.log'}\n"
            f"And the generated TeX (often points to the offending notebook section):\n"
            f"  - {temp_project_root / 'underwater-systems' / 'index_pdf_tex' / 'index.tex'}\n"
        )

    # Prefer export_pdf if valid
    if is_pdf_valid(export_pdf):
        print(f"[ok] Found exported PDF: {export_pdf}")
        return export_pdf

    # Fallback: temp pdf if valid
    if temp_pdf and is_pdf_valid(temp_pdf):
        print(f"[warn] Exported PDF invalid, using temp PDF: {temp_pdf}")
        return temp_pdf

    raise RuntimeError(
        "PDF not found or invalid after build.\n"
        f"Tried:\n"
        f"  - {export_pdf}\n"
        f"  - {temp_pdf}\n"
    )

def build_pdf_in_temp_with_myst(
    temp_project_root: Path,
    strict: bool,
    debug: bool,
    expected_output_rel: str | None,
    clean_templates_cache: bool = False,
) -> Path:
    """
    Build PDF via MyST CLI from TEMP project root:
        myst build --pdf [--strict] [--debug]

    If expected_output_rel is provided (e.g. "_build/exports/underwater-systems.pdf"),
    we return that file if it exists and passes sanity checks; otherwise we fall back
    to searching for the newest PDF under _build/exports.
    """
    if clean_templates_cache:
        run(["myst", "clean", "--templates", "--cache", "-y"], cwd=temp_project_root)

    cmd = ["myst", "build", "--pdf"]
    if strict:
        cmd += ["--strict"]
    if debug:
        cmd += ["--debug"]

    print("\nBuilding PDF in temp folder (MyST):")
    print(f"  CWD: {temp_project_root}")
    run(cmd, cwd=temp_project_root)

    # Prefer explicit output path if we set it in myst.yml
    if expected_output_rel:
        export_pdf = temp_project_root / expected_output_rel
        if is_pdf_valid(export_pdf):
            print(f"[ok] Found exported PDF: {export_pdf}")
            return export_pdf

    # Fallback: find newest PDF under _build/exports
    exports_dir = temp_project_root / "_build" / "exports"
    if exports_dir.exists():
        pdfs = list(exports_dir.rglob("*.pdf"))
        if pdfs:
            newest = max(pdfs, key=lambda p: p.stat().st_mtime)
            if is_pdf_valid(newest):
                print(f"[ok] Found newest exported PDF: {newest}")
                return newest

    # If we got here, treat as failure
    myst_dir = find_latest_myst_build_folder(temp_project_root)
    if myst_dir and latex_logs_have_errors(myst_dir):
        raise RuntimeError(
            "LaTeX reported errors during PDF build.\n"
            f"Check logs:\n"
            f"  - {myst_dir / 'index.shell.log'}\n"
            f"  - {myst_dir / 'index.log'}\n"
        )

    raise RuntimeError("PDF not found or invalid after myst build.")


def copy_pdf_back(pdf_path: Path, real_project_root: Path, out_name: str) -> Path:
    out_dir = real_project_root / "nbs" / "_build" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / out_name
    shutil.copy2(pdf_path, out_pdf)
    print(f"[ok] Copied PDF back to: {out_pdf}")
    return out_pdf


def copy_logs_back(temp_project_root: Path, real_project_root: Path) -> None:
    """
    Copy the Myst temp folder logs into real repo for easy debugging.
    """
    myst_dir = find_latest_myst_build_folder(temp_project_root)
    if not myst_dir:
        return
    dst = real_project_root / "nbs" / "_build" / "exports" / "myst_logs_latest"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)

    for name in ["index.log", "index.shell.log"]:
        src = myst_dir / name
        if src.exists():
            shutil.copy2(src, dst / name)

    # also helpful: generated TeX folder
    tex_dir = temp_project_root / "underwater-systems" / "index_pdf_tex"
    if tex_dir.exists():
        shutil.copytree(tex_dir, dst / "index_pdf_tex", dirs_exist_ok=True)

    print(f"[warn] Copied logs/tex to: {dst}")


def safety_checks(project_root: Path, temp_root: Path, temp_project_root: Path) -> None:
    project_root = project_root.resolve()
    temp_root = temp_root.resolve()
    temp_project_root = temp_project_root.resolve()

    if project_root == temp_project_root:
        raise RuntimeError("Refusing to rsync: project_root equals temp_project_root.")
    if temp_root in project_root.parents or project_root == temp_root:
        raise RuntimeError("Refusing to rsync: project_root is inside temp_root.")
    if project_root in temp_project_root.parents:
        raise RuntimeError("Refusing to rsync: temp_project_root is inside project_root.")
    if project_root in temp_root.parents:
        raise RuntimeError("Refusing to rsync: temp_root is inside project_root.")

   

def patch_myst_yml_template_OLD(temp_project_root: Path):
    """
    Forza il percorso del template nel myst.yml temporaneo 
    usando il percorso assoluto della cartella rsyncata.
    """
    myst_path = temp_project_root / "myst.yml" # o il percorso corretto nel tuo temp
    if not myst_path.exists():
        print(f"[warn] myst.yml non trovato in {myst_path}")
        return

    tpl_folder = temp_project_root / "_templates" / "tex" / "plain_latex"
    
    content = myst_path.read_text(encoding="utf-8")
    # Sostituisce la riga del template con il percorso assoluto temporaneo
    new_content = re.sub(
        r"(template:\s*).*(\s*)", 
        f"template: {tpl_folder.as_posix()}\n", 
        content
    )
    myst_path.write_text(new_content, encoding="utf-8")
    print(f"[ok] Patchato myst.yml per usare il template: {tpl_folder}")

def patch_myst_yml_template(temp_project_root: Path, template_rel: str = "_templates/tex/underwater_systems_latex") -> None:
    """
    TEMP ONLY: Ensure myst.yml has project.exports[*].template set for the PDF export.
    Works with Jupyter Book v2 (MyST) which reads template from myst.yml exports.
    """
    import yaml

    myst_path = temp_project_root / "myst.yml"
    if not myst_path.exists():
        raise FileNotFoundError(f"Cannot find myst.yml at: {myst_path}")

    tpl_folder = (temp_project_root / template_rel).resolve()
    if not tpl_folder.exists():
        raise RuntimeError(f"Template folder not found in TEMP: {tpl_folder}")

    data = yaml.safe_load(myst_path.read_text(encoding="utf-8")) or {}
    data.setdefault("project", {})
    project = data["project"]

    exports = project.get("exports")
    if exports is None:
        exports = []
        project["exports"] = exports

    if not isinstance(exports, list):
        raise RuntimeError("myst.yml: project.exports must be a list")

    # Find an existing PDF export entry
    pdf_entry = None
    for e in exports:
        if isinstance(e, dict) and str(e.get("format", "")).startswith("pdf"):
            pdf_entry = e
            break

    # If none exists, create one
    if pdf_entry is None:
        pdf_entry = {"format": "pdf"}
        exports.append(pdf_entry)

    # IMPORTANT: use a path that will work when CWD=temp_project_root
    # You can store relative path:
    pdf_entry["template"] = template_rel
    # pdf_entry["template"] = "../_templates/tex/plain_latex"

    myst_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"[ok] Patched myst.yml exports to use template: {template_rel}")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "nbs_root",
        nargs="?",
        default=".",
        help="Path to nbs folder (default: current dir). Typically run from nbs/ as `python bin/build_jupyterbook_with_images.py .`",
    )
    ap.add_argument("--temp-root", default=str(Path.home() / "tmp"), help="Temp parent folder (default: ~/tmp)")
    ap.add_argument("--temp-name", default=None, help="Temp project folder name (default: project folder name)")
    ap.add_argument("--no-image-copy", action="store_true", help="Skip copying nbs/images -> nbs/_build/html/images")
    ap.add_argument("--no-sanitize", action="store_true", help="Skip notebook sanitization in temp (videos/etc.)")
    ap.add_argument("--no-pagebreaks", action="store_true", help="Skip prepending page breaks in temp")
    ap.add_argument("--strict", action="store_true", help="Pass --strict to jupyter book build")
    ap.add_argument("--debug", action="store_true", help="Pass --debug to jupyter book build")
    ap.add_argument("--keep-temp", action="store_true", help="Do not delete temp project folder at the end")
    ap.add_argument("--output", default="underwater-systems.pdf", help="Name for copied-back PDF (e.g. underwater-systems.pdf)")
    #ap.add_argument("--template", default="_templates/tex/plain_latex", help="Forces myst to use a local template")
    args = ap.parse_args()

    # Resolve nbs + project root (nbdev convention: project_root/nbs)
    nbs_root = Path(args.nbs_root).expanduser().resolve()

    if nbs_root.name == "nbs":
        project_root = nbs_root.parent.resolve()
    else:
        if (nbs_root / "nbs").exists():
            project_root = nbs_root.resolve()
            nbs_root = (project_root / "nbs").resolve()
        else:
            project_root = nbs_root.resolve()

    if not (project_root / "nbs").exists():
        raise RuntimeError(f"Could not find nbs/ under: {project_root}. Run this from nbs/ or from project root.")
    nbs_root = (project_root / "nbs").resolve()

    print(f"NBS root is: {nbs_root}")
    print(f"Project root (one level above nbs) is: {project_root}")

    if not args.no_image_copy:
        copy_images_for_html(nbs_root)

    temp_root = Path(args.temp_root).expanduser().resolve()
    temp_name = args.temp_name or project_root.name
    temp_project_root = (temp_root / temp_name).resolve()

    safety_checks(project_root, temp_root, temp_project_root)

    try:
        # 1) rsync entire repo to temp
        rsync_to_temp(project_root, temp_project_root)

        temp_nbs = temp_project_root / "nbs"
        if not temp_nbs.exists():
            raise RuntimeError(f"Temp nbs folder not found at: {temp_nbs} (rsync failed?)")

        # 2) sanitize notebooks (TEMP ONLY) into nbs_pdf
        temp_nbs_pdf = temp_project_root / "nbs_pdf"

        if not args.no_sanitize:
            print("\nSanitizing notebooks in TEMP (videos removed/annotated for PDF)...")
            if temp_nbs_pdf.exists():
                shutil.rmtree(temp_nbs_pdf, ignore_errors=True)

            create_pdf_safe_version(str(temp_nbs), str(temp_nbs_pdf))

            # patch leftover patterns that break PDF
            patch_all_notebooks(temp_nbs_pdf)

            # patch myst.yml in temp to point to nbs_pdf/
            patch_temp_myst_yml(temp_project_root)

            #!!!
            print(f"\nLoading MySt template from {temp_project_root}")
            patch_myst_yml_template(temp_project_root)


            ## TO USE MYST - I HAVE NEVER TRIED IT AND IAM NOT SURE IF IT WORKS.
            # # Set PDF output inside TEMP so we can find it deterministically
            # pdf_output_rel = f"_build/exports/{args.output}"
            
            # patch_myst_yml_template(
            #     temp_project_root,
            #     template_rel="_templates/tex/underwater_systems_latex",
            #     pdf_output_rel=pdf_output_rel,
            # )
            # pdf_path = build_pdf_in_temp_with_myst(
            #     temp_project_root=temp_project_root,
            #     strict=args.strict,
            #     debug=args.debug,
            #     expected_output_rel=pdf_output_rel,
            #     clean_templates_cache=False,  # set True if you keep temp and swap templates often
            # )
            
        else:
            print("\n[warn] Skipping sanitization; build will use original notebooks.")
            # still patch myst if you want to point to nbs/ as-is (no change needed)

        # 3) page breaks on the folder we will build (TEMP ONLY)
        if not args.no_pagebreaks and temp_nbs_pdf.exists():
            print("\nPrepending page breaks in TEMP notebooks...")
            prepend_page_breaks_in_dir(str(temp_nbs_pdf))

        # 4) build PDF in TEMP (call 'nbs' because myst.yml lives in nbs/)        
        pdf_path = build_pdf_in_temp(
            temp_project_root=temp_project_root,
            strict=args.strict,
            debug=args.debug,
            output=args.output,
            template=None, #args.template
        )

        # 5) copy PDF back to REAL repo
        copy_pdf_back(pdf_path, project_root, out_name=args.output)

    except Exception as e:
        # copy logs back for debugging
        copy_logs_back(temp_project_root, project_root)
        raise
    finally:
        print("Cleaning up...")
        if temp_project_root.exists() and not args.keep_temp:
            print(f"\nCleaning temp folder: {temp_project_root}")
            shutil.rmtree(temp_project_root, ignore_errors=True)
        else:
            print(f"\nTemp folder: {temp_project_root}, was NOT removed.")
        print("DONE.")
        print("\n\n\nIMPORTANT:\n if the pdf is not to your liking, you can refine it using Myst directly. You can do so running: \n   myst clean --templates --cache -y\n   myst build --pdf --debug\n from the temp folder (use this script with option --keep-temp).\n")


if __name__ == "__main__":
    main()
