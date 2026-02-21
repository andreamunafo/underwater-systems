"""
1. rsyncs your whole project to a temp folder (no spaces),

2. in the temp copy only:

   - optionally prepends page breaks,
   - removes / replaces video embeds (both IPython.display.Video(...) code cells and HTML <video ...> / <img src="...mp4"> patterns),
   - inserts a placeholder like: “Video available in the online version (ctd_1280x720.mp4)”
   - runs jupyter book build --pdf from the temp project root,
   - finds the produced PDF.


   2) How to use it

From your nbs/ folder (or wherever), run:

`python bin/build_pdf_temp_pdfsafe.py --strict --debug --copy-pdf-back`

If you want only sanitization + PDF build (no page breaks):

`python bin/build_pdf_temp_pdfsafe.py --skip-pagebreaks --strict --debug`

If you ever want to test the build without sanitizing (not recommended for PDF):

`python bin/build_pdf_temp_pdfsafe.py --skip-sanitize --strict --debug`


"""

#!/usr/bin/env python3
import os
import re
import json
import shutil
import argparse
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional


# ----------------------------
# Rsync project to temp
# ----------------------------
def rsync_to_temp_project(src_project_root: str, temp_base: str = "~/tmp") -> Path:
    src = Path(src_project_root).resolve()
    temp_base_path = Path(os.path.expanduser(temp_base)).resolve()
    temp_base_path.mkdir(parents=True, exist_ok=True)

    dest = temp_base_path / src.name
    dest.mkdir(parents=True, exist_ok=True)

    cmd = ["rsync", "-av", "--delete", f"{str(src)}/", f"{str(dest)}/"]
    print("\nRsyncing project to temp path (no spaces recommended):")
    print("  SRC :", src)
    print("  DEST:", dest)
    subprocess.run(cmd, check=True)
    return dest


# ----------------------------
# PDF-safe notebook transforms
# ----------------------------
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
VIDEO_PAT = re.compile(r"(" + "|".join([re.escape(x) for x in VIDEO_EXTS]) + r")\b", re.IGNORECASE)

HTML_VIDEO_BLOCK_RE = re.compile(r"<video\b.*?>.*?</video>", re.IGNORECASE | re.DOTALL)
HTML_VIDEO_TAG_RE = re.compile(r"<video\b.*?>", re.IGNORECASE)
HTML_VIDEO_END_RE = re.compile(r"</video>", re.IGNORECASE)

# catches <img src="./videos/foo.mp4" ...>
HTML_IMG_VIDEO_SRC_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+?)['\"][^>]*>",
    re.IGNORECASE | re.DOTALL
)

# catches markdown links/images to video files: [x](./videos/a.mp4) or ![](a.mp4)
MD_LINK_RE = re.compile(r"\(([^)]+)\)")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _extract_video_filenames(text: str) -> List[str]:
    # pull likely filenames from occurrences like ./videos/foo.mp4 or foo.mp4
    found = []
    for ext in VIDEO_EXTS:
        # rough but effective: capture last path segment ending in ext
        for m in re.finditer(r"([A-Za-z0-9._\-/]+%s)" % re.escape(ext), text, flags=re.IGNORECASE):
            path = m.group(1)
            found.append(Path(path).name)
    # de-dup preserve order
    seen = set()
    out = []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _video_placeholder(filenames: List[str]) -> str:
    if filenames:
        joined = ", ".join(filenames)
        return f"**Video available in the online version** ({joined})."
    return "**Video available in the online version.**"


def sanitize_notebook_inplace(nb_path: Path) -> Tuple[bool, List[str]]:
    """
    Modifies notebook IN PLACE:
      - removes code cells that embed IPython Video(...)
      - replaces HTML <video>...</video> blocks with a placeholder
      - replaces <img src="...mp4"> (and other video-ext) with a placeholder
      - replaces markdown links to mp4/mov/etc with a placeholder line
    Returns (changed, notes)
    """
    raw = nb_path.read_text(encoding="utf-8")
    nb = json.loads(raw)

    changed = False
    notes: List[str] = []

    new_cells = []
    for idx, cell in enumerate(nb.get("cells", [])):
        ctype = cell.get("cell_type")
        src = cell.get("source", [])
        if isinstance(src, str):
            src_text = src
            src_list = [src]
        else:
            src_list = src
            src_text = "".join(src_list)

        # 1) Remove code-cell Video embeds
        if ctype == "code":
            # common patterns
            if (
                "IPython.display" in src_text
                and ("Video(" in src_text or "YouTubeVideo(" in src_text)
            ) or ("Video(" in src_text and "./videos/" in src_text):
                changed = True
                vids = _extract_video_filenames(src_text)
                notes.append(f"Removed code video cell #{idx} ({', '.join(vids) if vids else 'unknown video'})")
                # Replace with a markdown note cell (keeps context in PDF)
                md_note = _video_placeholder(vids)
                new_cells.append({
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": [md_note + "\n"],
                })
                continue

        # 2) Replace HTML video blocks in markdown/raw
        if ctype in ("markdown", "raw"):
            vids = _extract_video_filenames(src_text)

            # Replace full <video>...</video> blocks
            if HTML_VIDEO_BLOCK_RE.search(src_text):
                src_text2 = HTML_VIDEO_BLOCK_RE.sub(_video_placeholder(vids), src_text)
                if src_text2 != src_text:
                    changed = True
                    notes.append(f"Replaced <video> block in cell #{idx} ({', '.join(vids) if vids else 'unknown'})")
                    src_text = src_text2

            # Replace stray <video ...> and </video>
            if HTML_VIDEO_TAG_RE.search(src_text) or HTML_VIDEO_END_RE.search(src_text):
                src_text2 = HTML_VIDEO_TAG_RE.sub("", src_text)
                src_text2 = HTML_VIDEO_END_RE.sub("", src_text2)
                if src_text2 != src_text:
                    changed = True
                    notes.append(f"Stripped <video> tag(s) in cell #{idx} ({', '.join(vids) if vids else 'unknown'})")
                    # if we removed tags but left content, add a note above
                    src_text = _video_placeholder(vids) + "\n\n" + src_text2.strip()

            # Replace <img src="...mp4"> kind of hacks
            # We do this only when src is a video extension
            def _img_repl(m):
                nonlocal changed
                url = m.group(1)
                if VIDEO_PAT.search(url):
                    changed = True
                    name = Path(url).name
                    notes.append(f"Replaced <img src=video> in cell #{idx} ({name})")
                    return _video_placeholder([name])
                return m.group(0)

            src_text2 = HTML_IMG_VIDEO_SRC_RE.sub(_img_repl, src_text)
            src_text = src_text2

            # Replace markdown links/images pointing to video files
            # If any ( ...mp4 ) occurrences exist, replace the whole line with placeholder
            # (simple approach: if markdown cell references mp4/mov, append placeholder and remove links)
            if any(ext.lower() in src_text.lower() for ext in VIDEO_EXTS):
                # find markdown link targets
                md_targets = MD_LINK_RE.findall(src_text)
                md_vids = []
                for t in md_targets:
                    if VIDEO_PAT.search(t):
                        md_vids.append(Path(t).name)
                if md_vids:
                    changed = True
                    notes.append(f"Replaced markdown video link(s) in cell #{idx} ({', '.join(md_vids)})")
                    # remove just the video links, keep other text
                    for v in md_vids:
                        # remove occurrences like (./videos/foo.mp4)
                        src_text = re.sub(r"\([^)]*%s[^)]*\)" % re.escape(v), "()", src_text)
                    # add a note at the end
                    src_text = src_text.rstrip() + "\n\n" + _video_placeholder(md_vids) + "\n"

            # write back as list of lines
            cell["source"] = [src_text] if isinstance(src, str) else src_text.splitlines(True)

        new_cells.append(cell)

    if changed:
        nb["cells"] = new_cells
        nb_path.write_text(json.dumps(nb, indent=2, ensure_ascii=False), encoding="utf-8")

    return changed, notes


def prepend_page_break_to_notebook_inplace(nb_path: Path) -> bool:
    with nb_path.open("r", encoding="utf-8") as f:
        nb = json.load(f)

    page_break_cell = {
        "cell_type": "raw",
        "metadata": {},
        "source": ['<div style="page-break-before: always;"></div>'],
    }

    if nb.get("cells") and nb["cells"][0].get("source") != page_break_cell["source"]:
        nb["cells"].insert(0, page_break_cell)
        nb_path.write_text(json.dumps(nb, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    return False


def process_notebooks_in_temp(nbs_dir: Path, do_pagebreaks: bool, do_sanitize: bool) -> None:
    ipynbs = sorted(nbs_dir.rglob("*.ipynb"))
    print(f"\nProcessing notebooks in temp: {len(ipynbs)} found under {nbs_dir}")

    for nb in ipynbs:
        if do_pagebreaks:
            if prepend_page_break_to_notebook_inplace(nb):
                print(f"  + pagebreak: {nb.relative_to(nbs_dir)}")

        if do_sanitize:
            changed, notes = sanitize_notebook_inplace(nb)
            if changed:
                print(f"  + pdf-safe: {nb.relative_to(nbs_dir)}")
                for n in notes[:6]:
                    print(f"      - {n}")
                if len(notes) > 6:
                    print(f"      - ... ({len(notes)-6} more)")


# ----------------------------
# Build PDF in temp
# ----------------------------
def run_jupyter_book_pdf(project_root: Path, extra_args: Optional[List[str]] = None) -> None:
    extra_args = extra_args or []
    print("\nBuilding PDF in temp folder:")
    print("  CWD:", project_root)

    subprocess.run(["jupyter", "book", "--version"], check=True, cwd=str(project_root))
    # IMPORTANT: do NOT pass "nbs" here, otherwise myst tries to read it as a file and you get EISDIR.
    subprocess.run(["jupyter", "book", "build", "--pdf", "-v", *extra_args], check=True, cwd=str(project_root))


def find_pdf_outputs(project_root: Path) -> List[Path]:
    candidates = []

    # common places
    candidates.extend(project_root.glob("_build/exports/*.pdf"))
    candidates.extend(project_root.glob("underwater-systems/*.pdf"))
    candidates.extend(project_root.glob("_build/**/*.pdf"))

    # de-dup + filter existing
    uniq = []
    seen = set()
    for p in candidates:
        if p.exists() and p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                uniq.append(rp)
    return uniq


def main():
    parser = argparse.ArgumentParser(
        description="Rsync project to temp, make notebooks PDF-safe (remove videos), then build Jupyter Book PDF."
    )

    script_dir = Path(__file__).resolve().parent

    # nbs/bin/... -> nbs -> project root is one above nbs
    nbs_root = script_dir.parent
    project_root = nbs_root.parent

    parser.add_argument("--src-project-root", default=str(project_root), help="Original project root (one above nbs/).")
    parser.add_argument("--temp-base", default="~/tmp", help="Temp base directory.")
    parser.add_argument("--skip-pagebreaks", action="store_true", help="Do not prepend page breaks (in temp).")
    parser.add_argument("--skip-sanitize", action="store_true", help="Do not remove/replace videos (in temp).")
    parser.add_argument("--strict", action="store_true", help="Pass --strict to jupyter book build.")
    parser.add_argument("--debug", action="store_true", help="Pass --debug to jupyter book build.")
    parser.add_argument("--copy-pdf-back", action="store_true", help="Copy found PDFs back to original project under nbs/underwater-systems/")

    args = parser.parse_args()

    src_project_root = Path(args.src_project_root).resolve()
    temp_project_root = rsync_to_temp_project(str(src_project_root), temp_base=args.temp_base)

    temp_nbs = temp_project_root / "nbs"
    if not temp_nbs.exists():
        raise FileNotFoundError(f"Temp nbs folder not found: {temp_nbs}")

    process_notebooks_in_temp(
        temp_nbs,
        do_pagebreaks=not args.skip_pagebreaks,
        do_sanitize=not args.skip_sanitize,
    )

    jb_args = []
    if args.strict:
        jb_args.append("--strict")
    if args.debug:
        jb_args.append("--debug")

    run_jupyter_book_pdf(temp_project_root, extra_args=jb_args)

    pdfs = find_pdf_outputs(temp_project_root)
    if not pdfs:
        raise FileNotFoundError("No PDFs found after build. Check _build/ for logs and outputs.")

    print("\nPDF(s) found:")
    for p in pdfs[:20]:
        print(" -", p)

    if args.copy_pdf_back:
        dest_dir = src_project_root / "nbs" / "underwater-systems"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for p in pdfs:
            out = dest_dir / p.name
            shutil.copy2(str(p), str(out))
        print("\nCopied PDFs back to:", dest_dir)


if __name__ == "__main__":
    main()
