import json, re
from pathlib import Path

NBS = Path("nbs")

def bad_table_html(s: str) -> bool:
    if "<table" not in s.lower():
        return False
    # HTML table with no <tr> and no <td>/<th> is almost certainly invalid for LaTeX conversion
    sl = s.lower()
    if "<table" in sl and "</table>" in sl:
        has_tr = "<tr" in sl
        has_cell = "<td" in sl or "<th" in sl
        if not has_tr and not has_cell:
            return True
    return False

def find_bad_tables(nb_path: Path):
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    hits = []
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "markdown":
            continue
        src = "".join(cell.get("source", ""))
        if bad_table_html(src):
            snippet = src.strip().replace("\n"," ")[:200]
            hits.append((i, snippet))
    return hits

all_hits = []
for nb_path in sorted(NBS.rglob("*.ipynb")):
    hits = find_bad_tables(nb_path)
    if hits:
        all_hits.append((nb_path, hits))

if not all_hits:
    print("✅ No obvious HTML <table> blocks without rows/cells found.")
else:
    print("⛔ Found likely-bad HTML tables:")
    for nb_path, hits in all_hits:
        print(f"\nFILE: {nb_path}")
        for idx, snip in hits:
            print(f"  - cell {idx}: {snip}")
