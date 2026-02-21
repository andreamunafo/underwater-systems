"""
Creates a PDF-friendly version of notebooks by copying a folder and sanitizing .ipynb files.

What it does (in the COPIED folder only):
- Keeps normal images (png/jpg/jpeg/webp/pdf) so figures remain in the PDF.
- Removes/replaces risky media that breaks TeX/PDF builds: gif, mp4, mov, avi
- Unwraps HTML <table>...</table> blocks that contain <img>/<video> into plain Markdown:
    - safe images become Markdown ![](...)
    - risky media becomes an admonition placeholder with the media filename
- Removes ```{only} html ...``` blocks (PDF should not need them)
- Replaces IPython.display.Video("...") code cells with a placeholder (PDF-safe)

Usage:
From the project_root (normally `./Sistemi Subacquei/website-underwater-systems`) run:

  python nbs/bin/build_jupyterbook_with_images.py . --strict --debug --output underwater-systems.pdf 

"""

import json
import re
import shutil
from pathlib import Path


# -----------------------
# Patterns
# -----------------------
TABLE_PATTERN = re.compile(r"(?is)<table\b.*?</table>")
VIDEO_TAG_PATTERN = re.compile(r"(?is)<video\b.*?</video>")
ONLY_HTML_BLOCK_PATTERN = re.compile(r"(?is)```{only}\s+html.*?```")

# <img ... src="...">  (capture full src path)
IMG_TAG_PATTERN = re.compile(r'(?is)<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>')
ALT_IN_IMG_PATTERN = re.compile(r'(?is)\balt=["\']([^"\']*)["\']')

# <img ... src="...mp4/mov/avi"...> (rare but possible)
IMG_VIDEO_PATTERN = re.compile(r'(?is)<img[^>]+src=["\']([^"\']+\.(mp4|mov|avi))["\'][^>]*>')

# Detect IPython.display.Video("...") usage in code cells
IPY_VIDEO_CALL_PATTERN = re.compile(r'Video\(\s*["\'](.+?)["\']', re.IGNORECASE)

# Risky media extensions for PDF/TeX build
RISKY_MEDIA_EXT_PATTERN = re.compile(r"\.(gif|mp4|mov|avi)\b", re.IGNORECASE)

# Extract a representative src="..." inside arbitrary HTML blocks
HTML_SRC_ANY_PATTERN = re.compile(r'(?is)\bsrc=["\']([^"\']+)["\']')

# Remove auto-TOC code cells in PDF build
TOC_IMPORT_PATTERN = re.compile(r"^\s*from\s+bin\.toc\s+import\s+display_table_of_contents\s*$", re.MULTILINE)
TOC_CALL_PATTERN = re.compile(r"^\s*display_table_of_contents\s*\(.*?\)\s*$", re.MULTILINE | re.DOTALL)

# -----------------------
# Helpers
# -----------------------
def is_risky_media(path: str) -> bool:
    return bool(RISKY_MEDIA_EXT_PATTERN.search(path or ""))


def make_media_placeholder(label: str, media_path: str) -> str:
    """
    label: short description shown in the admonition
    media_path: path as found in notebook (used to show filename)
    """
    filename = Path(media_path).name if media_path else "media"
    return (
        "\n\n"
        "```{admonition} Media available online\n"
        ":class: note\n\n"
        f"{label}\n\n"
        f"**{filename}**  \n"
        "This media is available in the online version of these notes.\n"
        "```\n\n"
    )


def extract_alt_from_img_tag(img_tag: str) -> str:
    m = ALT_IN_IMG_PATTERN.search(img_tag)
    if not m:
        return ""
    return (m.group(1) or "").strip()


def unwrap_table_to_markdown(block: str) -> str:
    """
    Convert an HTML <table> block that contains <img>/<video> into PDF-safe markdown:
    - safe images -> ![alt](src)
    - risky media (gif/mp4/mov/avi) -> placeholder admonition
    """
    # Grab all <img ...> tags in the table (as tags, not just src)
    img_tags = list(re.finditer(r"(?is)<img\b[^>]*>", block))

    parts = []

    # Handle <video> tag presence inside table
    if "<video" in block.lower():
        src_m = HTML_SRC_ANY_PATTERN.search(block)
        src = src_m.group(1) if src_m else "video"
        parts.append(make_media_placeholder("Video removed for PDF (see online notes)", src))

    # Handle <img ...> tags
    for im in img_tags:
        tag = im.group(0)
        src_m = re.search(r'(?is)\bsrc=["\']([^"\']+)["\']', tag)
        src = src_m.group(1) if src_m else ""
        if not src:
            continue

        alt = extract_alt_from_img_tag(tag) or Path(src).stem or "figure"

        if is_risky_media(src):
            parts.append(make_media_placeholder("Media removed for PDF (see online notes)", src))
        else:
            # keep safe images
            parts.append(f"![{alt}]({src})\n")

    if parts:
        return "\n\n" + "\n".join(parts) + "\n\n"

    # If we couldn't parse anything meaningful, fallback
    src_m = HTML_SRC_ANY_PATTERN.search(block)
    src = src_m.group(1) if src_m else "media"
    return make_media_placeholder("Media table removed for PDF (see online notes)", src)

def strip_html_outputs(cell: dict) -> bool:
    """
    Remove any code-cell outputs that contain HTML (widgets, animations, etc.)
    which Myst may try to render into TeX as an image and can produce
    \\includegraphics{} with an empty filename.
    Returns True if something was removed.
    """
    outs = cell.get("outputs", [])
    if not outs:
        return False

    new_outs = []
    changed = False

    for out in outs:
        data = out.get("data", {})
        # Some outputs store mime bundle in "data"
        if isinstance(data, dict) and "text/html" in data:
            changed = True
            continue

        # Sometimes HTML can appear in "text" (rare, but safe to guard)
        txt = out.get("text")
        if isinstance(txt, str) and "<div" in txt.lower() and "javascript" in txt.lower():
            changed = True
            continue

        new_outs.append(out)

    if changed:
        cell["outputs"] = new_outs
        # also clear execution count if you like (optional)
        # cell["execution_count"] = None

    return changed


# -----------------------
# Markdown sanitization
# -----------------------
def sanitize_markdown(text: str) -> str:
    # 0) Remove {only} html blocks entirely (PDF should not need them)
    text = ONLY_HTML_BLOCK_PATTERN.sub("", text)

    # 1) Replace raw <video>...</video> blocks anywhere
    def _replace_video_tag(match):
        block = match.group(0)
        src_m = re.search(r'(?is)\bsrc=["\']([^"\']+)["\']', block)
        src = src_m.group(1) if src_m else "video"
        return make_media_placeholder("Video removed for PDF (see online notes)", src)

    text = VIDEO_TAG_PATTERN.sub(_replace_video_tag, text)

    # 2) Replace <img ... src="...mp4/mov/avi"...> (embedded video referenced as img)
    def _replace_img_video(match):
        media_path = match.group(1)
        return make_media_placeholder("Embedded video reference removed for PDF (see online notes)", media_path)

    text = IMG_VIDEO_PATTERN.sub(_replace_img_video, text)

    # 3) Replace <table>...</table> blocks ONLY if they contain <img>/<video>
    #    and then unwrap to markdown (keeping safe images, replacing risky media).
    def _replace_table(match):
        block = match.group(0)
        if "<img" not in block.lower() and "<video" not in block.lower():
            return block

        # If the table contains any risky media OR is using tables for media layout,
        # unwrap to markdown (safe for TeX).
        # Even if only png/jpg, unwrapping is safer than HTML tables for TeX.
        return unwrap_table_to_markdown(block)

    text = TABLE_PATTERN.sub(_replace_table, text)

    return text


# -----------------------
# Notebook sanitization
# -----------------------
def sanitize_notebook(nb_path: Path):
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    new_cells = []

    for cell in nb.get("cells", []):
        ctype = cell.get("cell_type")

        # 1) Replace code cells that call Video("...") with a markdown placeholder
        if ctype == "code":
            # Remove HTML outputs (animations/widgets) that break TeX export
            strip_html_outputs(cell)
            
            src = "".join(cell.get("source", []))

            # --- Remove auto-TOC generator cells (PDF-safe copy only) ---
            if TOC_IMPORT_PATTERN.search(src) or TOC_CALL_PATTERN.search(src):
                # Option A: drop the cell entirely
                continue
                
            m = IPY_VIDEO_CALL_PATTERN.search(src)
            if m:
                video_path = m.group(1)
                new_cells.append(
                    {
                        "cell_type": "markdown",
                        "metadata": {},
                        "source": [make_media_placeholder("Video removed for PDF (see online notes)", video_path)],
                    }
                )
                continue

        # 2) Sanitize markdown cells
        if ctype == "markdown":
            text = "".join(cell.get("source", []))
            cleaned = sanitize_markdown(text)
            cell["source"] = [cleaned]

        new_cells.append(cell)

    nb["cells"] = new_cells
    nb_path.write_text(json.dumps(nb, indent=2), encoding="utf-8")
    print(f"Sanitized: {nb_path}")


# -----------------------
# Folder copy + run
# -----------------------
def copy_tree(src: Path, dest: Path):
    if dest.exists():
        shutil.rmtree(dest)

    # Copy everything but skip irrelevant junk
    def ignore(dirpath, names):
        skip = {".ipynb_checkpoints", "_build", "__pycache__", ".DS_Store"}
        return [n for n in names if n in skip]

    shutil.copytree(src, dest, ignore=ignore)


def create_pdf_safe_version(src_dir: str, dest_dir: str):
    src = Path(src_dir).resolve()
    dest = Path(dest_dir).resolve()

    copy_tree(src, dest)

    for nb in dest.rglob("*.ipynb"):
        sanitize_notebook(nb)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("src", help="Original notebooks directory (e.g. nbs)")
    parser.add_argument("dest", help="Destination PDF-safe directory (e.g. _pdf_nbs)")
    args = parser.parse_args()

    create_pdf_safe_version(args.src, args.dest)
