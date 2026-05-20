"""
generate_docs.py

Reads changelog.md and processed.json, then for each unprocessed entry:
  - CREATED / MODIFIED : generate .md doc + convert PDF to PNGs → wiki
  - DELETED            : remove .md + PNGs from wiki

Run from the root of the main repo:
    python generate_docs.py \
        --main-repo "." \
        --wiki-repo "../wiki" \
        --powerbi-root "PowerBI"
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import fitz  # pymupdf
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False
    print("Warning: pymupdf not available — PDF conversion will be skipped.")

POINT_OF_CONTACT = "analytics@radiusgs.com"

METADATA_KEYWORDS = {
    "lineageTag", "annotation", "formatString", "extendedProperty",
    "summarizeBy", "dataType", "sourceColumn", "isHidden", "isPrivate",
    "displayFolder", "sortByColumn", "mode", "dataCategory",
    "isNameInferred", "showAsVariationsOnly", "variation", "hierarchy",
}


# ---------------------------------------------------------------------------
# PDF to PNG
# ---------------------------------------------------------------------------

def pdf_to_pngs(pdf_path: Path, workspace: str, report_name: str, wiki_dir: Path):
    """
    Convert each page of the PDF to a PNG in the wiki directory.
    Returns list of (page_label, png_filename) tuples.
    Returns empty list if PDF missing or pymupdf unavailable.
    """
    if not FITZ_AVAILABLE:
        print("  Skipping PDF conversion — pymupdf not installed.")
        return []

    if not pdf_path.exists():
        print(f"  PDF not found at {pdf_path} — images will be empty placeholders.")
        return []

    results = []
    doc = fitz.open(pdf_path)
    for page_num, page in enumerate(doc, start=1):
        mat = fitz.Matrix(150 / 72, 150 / 72)
        pix = page.get_pixmap(matrix=mat)
        png_name = f"{workspace}_{report_name}_page{page_num}.png"
        out_path = wiki_dir / png_name
        pix.save(out_path)
        print(f"  Saved PNG: {out_path}")
        results.append((f"Page {page_num}", png_name))
    doc.close()
    return results


# ---------------------------------------------------------------------------
# M-expression parser
# ---------------------------------------------------------------------------

def parse_m_expression(expression: str):
    if not isinstance(expression, str):
        return {"Source Type": None, "Database": None, "Schema": None, "Table": None}

    if "Table.FromRows" in expression or "Binary.Decompress" in expression:
        return {"Source Type": "Hardcoded Embedded Table", "Database": None, "Schema": None, "Table": None}

    if "Sql.Database" in expression:
        db_match = re.search(r'Sql\.Database\s*\(\s*"[^"]+"\s*,\s*"([^"]+)"', expression)
        database = db_match.group(1) if db_match else None
        if not database:
            db_match2 = re.search(r'\[Name\s*=\s*"([^"]+)"\]', expression)
            database = db_match2.group(1) if db_match2 else None

        st_match = re.search(r'\[Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"\]', expression)
        schema = st_match.group(1) if st_match else None
        table  = st_match.group(2) if st_match else None
        return {"Source Type": "SQL", "Database": database, "Schema": schema, "Table": table}

    return {"Source Type": "Unknown", "Database": None, "Schema": None, "Table": None}


# ---------------------------------------------------------------------------
# TMDL parser
# ---------------------------------------------------------------------------

def parse_tmdl(tmdl_path: Path):
    text  = tmdl_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    table_name = None
    for line in lines:
        m = re.match(r"^table\s+'?([^'\n]+)'?\s*$", line.strip())
        if m:
            table_name = m.group(1).strip("'")
            break
    if table_name is None:
        return None

    is_hidden = any(
        lines[i].strip() in ("isHidden", "isPrivate", "showAsVariationsOnly")
        for i in range(min(10, len(lines)))
    )

    columns  = []
    measures = []
    m_source = None
    is_dax   = False
    dax_expr = None

    i = 0
    while i < len(lines):
        line = lines[i]

        col_match = re.match(r"^\tcolumn\s+'?(.+?)'?(?:\s*=.*)?\s*$", line)
        if col_match:
            col_name  = col_match.group(1).strip("'")
            data_type = None
            j = i + 1
            while j < len(lines) and (lines[j].startswith("\t\t") or lines[j].strip() == ""):
                dt = re.match(r"\s+dataType:\s+(\S+)", lines[j])
                if dt:
                    data_type = dt.group(1)
                    break
                j += 1
            columns.append({"TableName": table_name, "Column": col_name, "DataType": data_type or ""})
            i += 1
            continue

        meas_match = re.match(r"^\tmeasure\s+'?(.+?)'?\s*=\s*(.*)", line)
        if meas_match:
            meas_name  = meas_match.group(1).strip("'")
            first_part = meas_match.group(2).strip()
            expr_lines = [first_part] if first_part else []
            j = i + 1
            while j < len(lines):
                nl = lines[j]
                if nl.startswith("\t") and not nl.startswith("\t\t"):
                    kw = nl.strip().split()[0] if nl.strip() else ""
                    if kw in METADATA_KEYWORDS | {"column", "measure", "partition",
                                                   "hierarchy", "variation"}:
                        break
                if nl.startswith("\t\t"):
                    stripped   = nl.strip()
                    first_word = stripped.split(":")[0].split()[0] if stripped else ""
                    if first_word in METADATA_KEYWORDS:
                        break
                    expr_lines.append(stripped)
                j += 1
            full_expr = "\n".join(expr_lines).strip()
            measures.append({"TableName": table_name, "Measure": meas_name, "Expression": full_expr})
            i += 1
            continue

        part_match = re.match(r"^\tpartition\s+\S+\s*=\s*(calculated|m)\s*$", line)
        if part_match:
            partition_type = part_match.group(1)
            src_lines  = []
            in_source  = False
            j = i + 1
            while j < len(lines):
                pl = lines[j]
                if re.match(r"\s+source\s*=", pl):
                    in_source = True
                    after_eq  = re.sub(r".*source\s*=\s*", "", pl).strip()
                    if after_eq:
                        src_lines.append(after_eq)
                    j += 1
                    continue
                if in_source:
                    if pl.startswith("\t") and not pl.startswith("\t\t\t"):
                        kw = pl.strip().split()[0] if pl.strip() else ""
                        if kw in ("annotation", "partition", "column",
                                  "measure", "hierarchy", "mode"):
                            break
                    src_lines.append(pl.strip())
                j += 1

            raw = "\n".join(src_lines).strip()
            if partition_type == "calculated":
                is_dax   = True
                dax_expr = raw
            else:
                m_source = raw

            i = j
            continue

        i += 1

    return {
        "table_name": table_name,
        "is_hidden":  is_hidden,
        "is_dax":     is_dax,
        "dax_expr":   dax_expr,
        "m_source":   m_source,
        "columns":    columns,
        "measures":   measures,
    }


# ---------------------------------------------------------------------------
# Table data aggregator
# ---------------------------------------------------------------------------

def get_tables_data(tables_folder: Path):
    all_columns  = []
    all_measures = []
    sources      = []
    dax_tables   = []

    for tmdl_file in sorted(tables_folder.glob("*.tmdl")):
        result = parse_tmdl(tmdl_file)
        if result is None:
            continue

        tn        = result["table_name"]
        is_hidden = result["is_hidden"]

        if result["is_dax"] and result["dax_expr"]:
            dax_tables.append({"TableName": tn, "Expression": result["dax_expr"]})
            continue

        if is_hidden:
            continue

        all_columns.extend(result["columns"])
        all_measures.extend(result["measures"])

        if result["m_source"]:
            parsed = parse_m_expression(result["m_source"])
            sources.append({
                "TableName":   tn,
                "Source Type": parsed["Source Type"],
                "Database":    parsed["Database"] or "",
                "Schema":      parsed["Schema"]   or "",
                "Table":       parsed["Table"]    or "",
            })

    return all_columns, all_measures, sources, dax_tables


# ---------------------------------------------------------------------------
# Pages reader
# ---------------------------------------------------------------------------

def get_pages(report_folder: Path):
    report_json = report_folder / "report.json"
    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
        sections = data.get("sections", [])
        visible = []
        for s in sections:
            config = json.loads(s.get("config", "{}"))
            if config.get("visibility", 0) == 1:
                continue
            visible.append((s.get("ordinal", 0), s["displayName"]))
        visible.sort(key=lambda x: x[0])
        return [name for _, name in visible]
    except Exception as e:
        print(f"  Warning: could not read pages — {e}")
        return []


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def sanitize_cell(value):
    return str(value).replace("\r\n", "<br>").replace("\n", "<br>")


def rows_to_markdown(headers, rows):
    if not rows:
        return "_No data available._"
    clean_rows = [[sanitize_cell(cell) for cell in row] for row in rows]
    col_widths = [len(h) for h in headers]
    for row in clean_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells):
        return "| " + " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "| " + " | ".join("-" * w for w in col_widths) + " |"
    return "\n".join([fmt_row(headers), sep] + [fmt_row(r) for r in clean_rows])


def build_sources_md(sources):
    headers = ["TableName", "Source Type", "Database", "Schema", "Table"]
    rows = [[s["TableName"], s["Source Type"], s["Database"], s["Schema"], s["Table"]] for s in sources]
    return rows_to_markdown(headers, rows)


def build_dax_tables_md(dax_tables):
    headers = ["TableName", "Expression"]
    rows = [[d["TableName"], d["Expression"]] for d in dax_tables]
    return rows_to_markdown(headers, rows)


def build_schema_md(columns):
    headers = ["TableName", "Column", "DataType"]
    rows = [[c["TableName"], c["Column"], c["DataType"]] for c in columns]
    return rows_to_markdown(headers, rows)


def build_measures_md(measures):
    headers = ["TableName", "Measure", "Expression"]
    rows = [[m["TableName"], m["Measure"], m["Expression"]] for m in measures]
    return rows_to_markdown(headers, rows)


# ---------------------------------------------------------------------------
# Retain existing descriptions from wiki .md
# ---------------------------------------------------------------------------

def extract_existing_descriptions(md_path: Path):
    """
    Returns:
        report_description : str or None
        page_descriptions  : dict {page_name: description_text}
        users              : str or None
    """
    if not md_path.exists():
        return None, {}, None

    content = md_path.read_text(encoding="utf-8")

    # Report description
    report_desc = None
    desc_match = re.search(
        r"## Description\s*\n+(.*?)(?=\n---|\n## )",
        content, re.DOTALL
    )
    if desc_match:
        text = desc_match.group(1).strip()
        if text and text != "_Add report description here._":
            report_desc = text

    # Users
    users = None
    users_match = re.search(
        r"## Users\s*\n+(.*?)(?=\n---|\n## )",
        content, re.DOTALL
    )
    if users_match:
        text = users_match.group(1).strip()
        if text and text != "_Add users here._":
            users = text

    # Per-page descriptions
    page_descriptions = {}
    page_blocks = re.findall(
        r"-\s+(.+?)\n!\[.*?\]\(.*?\)\n(.*?)(?=\n-\s+|\n##|\Z)",
        content, re.DOTALL
    )
    for page_name, desc_block in page_blocks:
        desc = desc_block.strip()
        if desc and desc != "_Add page description here._":
            page_descriptions[page_name.strip()] = desc

    return report_desc, page_descriptions, users


# ---------------------------------------------------------------------------
# Generate a single .md doc
# ---------------------------------------------------------------------------

def generate_doc(
    workspace: str,
    report_name: str,
    wiki_dir: Path,
    png_list: list,
    pages: list,
    all_columns, all_measures, sources, dax_tables,
    existing_report_desc=None,
    existing_page_descs=None,
    existing_users=None,
):
    if existing_page_descs is None:
        existing_page_descs = {}

    lines = []
    lines.append(f"# {report_name}\n")
    lines.append("---\n")

    # Description
    lines.append("## Description\n")
    lines.append(existing_report_desc if existing_report_desc else "_Add report description here._")
    lines.append("\n---\n")

    # Pages
    lines.append("## Pages\n")
    if pages:
        for idx, page_name in enumerate(pages):
            lines.append(f"- {page_name}")
            if idx < len(png_list):
                _, png_filename = png_list[idx]
                encoded_png = png_filename.replace(" ", "%20")
                lines.append(f"![{page_name}]({encoded_png})")
            else:
                lines.append(f"![{page_name}]()")
            page_desc = existing_page_descs.get(page_name, "_Add page description here._")
            lines.append(page_desc)
            lines.append("")
    else:
        lines.append("_No pages found._\n")
    lines.append("---\n")

    # Sources
    lines.append("## Sources\n")
    lines.append(build_sources_md(sources))
    lines.append("\n---\n")

    # DAX Tables
    lines.append("## DAX Tables\n")
    lines.append(build_dax_tables_md(dax_tables))
    lines.append("\n---\n")

    # Schema
    lines.append("## Schema\n")
    lines.append(build_schema_md(all_columns))
    lines.append("\n---\n")

    # Measures
    lines.append("## Measures\n")
    lines.append(build_measures_md(all_measures))
    lines.append("\n---\n")

    # Users
    lines.append("## Users\n")
    lines.append(existing_users if existing_users else "_Add users here._")
    lines.append("\n---\n")

    # Point of Contact
    lines.append("## Point of Contact\n")
    lines.append(POINT_OF_CONTACT)
    lines.append("")

    output_path = wiki_dir / f"{report_name}.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Saved doc: {output_path}")


# ---------------------------------------------------------------------------
# Delete wiki files for a report
# ---------------------------------------------------------------------------

def delete_report_from_wiki(workspace: str, report_name: str, wiki_dir: Path):
    md_path = wiki_dir / f"{report_name}.md"
    if md_path.exists():
        md_path.unlink()
        print(f"  Deleted: {md_path}")
    else:
        print(f"  Skipping (not found): {md_path}")

    prefix = f"{workspace}_{report_name}_page"
    for png in wiki_dir.glob(f"{prefix}*.png"):
        png.unlink()
        print(f"  Deleted PNG: {png}")


# ---------------------------------------------------------------------------
# Home.md updater
# ---------------------------------------------------------------------------

def update_home_md(wiki_dir: Path):
    md_files = sorted([
        f.stem for f in wiki_dir.glob("*.md")
        if f.stem != "Home"
    ])

    lines = []
    lines.append("# Power BI Reports\n")
    lines.append("---\n")
    lines.append("## Reports\n")
    if md_files:
        for name in md_files:
            encoded = name.replace(" ", "%20")
            lines.append(f"- [{name}]({encoded})")
    else:
        lines.append("_No reports documented yet._")
    lines.append("")

    home_path = wiki_dir / "Home.md"
    home_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Updated Home.md")


# ---------------------------------------------------------------------------
# Changelog reader
# ---------------------------------------------------------------------------

def read_changelog(changelog_path: Path):
    if not changelog_path.exists():
        return {}

    content = changelog_path.read_text(encoding="utf-8")
    sections = re.split(r'(?=^## \d{4}-\d{2}-\d{2})', content, flags=re.MULTILINE)

    result = {}
    for section in sections:
        date_match = re.match(r"## (\d{4}-\d{2}-\d{2})", section)
        if not date_match:
            continue
        date = date_match.group(1)
        entries = {}
        for line in section.split("\n"):
            line = line.strip()
            if ":" in line and not line.startswith("#") and not line.startswith("-"):
                status, report = line.split(":", 1)
                status = status.strip()
                report = report.strip()
                if status in ("CREATED", "MODIFIED", "DELETED"):
                    entries[report] = status
        if entries:
            result[date] = entries

    return result


# ---------------------------------------------------------------------------
# Processed tracker
# ---------------------------------------------------------------------------

def read_processed(processed_path: Path):
    if not processed_path.exists():
        return {}
    try:
        return json.loads(processed_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_processed(processed_path: Path, date: str, report: str):
    data = read_processed(processed_path)
    if date not in data:
        data[date] = {}
    data[date][report] = "completed"
    processed_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_processed(processed: dict, date: str, report: str):
    return processed.get(date, {}).get(report) == "completed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-repo",    required=True, help="Path to main repo root")
    parser.add_argument("--wiki-repo",    required=True, help="Path to wiki repo root")
    parser.add_argument("--powerbi-root", required=True, help="PowerBI folder name inside main repo")
    args = parser.parse_args()

    main_repo    = Path(args.main_repo).resolve()
    wiki_dir     = Path(args.wiki_repo).resolve()
    powerbi_root = main_repo / args.powerbi_root

    changelog_path = main_repo / "changelog.md"
    processed_path = main_repo / "processed.json"

    changelog = read_changelog(changelog_path)
    processed = read_processed(processed_path)

    if not changelog:
        print("No changelog entries found.")
        return

    any_processed = False

    for date, entries in sorted(changelog.items()):
        for report_key, status in entries.items():
            if is_processed(processed, date, report_key):
                print(f"  Already processed [{date}] {report_key} — skipping.")
                continue

            parts = report_key.split("/", 1)
            if len(parts) != 2:
                print(f"  Skipping malformed entry: {report_key}")
                continue

            workspace, report_name = parts[0].strip(), parts[1].strip()
            pbip_folder = powerbi_root / workspace / report_name

            print(f"\n[{date}] {status}: {workspace} / {report_name}")

            try:
                if status in ("CREATED", "MODIFIED"):
                    report_folder = pbip_folder / f"{report_name}.Report"
                    tables_folder = pbip_folder / f"{report_name}.SemanticModel" / "definition" / "tables"
                    pdf_path      = pbip_folder / f"{report_name}.pdf"

                    # Retain descriptions if MODIFIED
                    existing_md = wiki_dir / f"{report_name}.md"
                    if status == "MODIFIED":
                        existing_report_desc, existing_page_descs, existing_users = \
                            extract_existing_descriptions(existing_md)
                    else:
                        existing_report_desc, existing_page_descs, existing_users = None, {}, None

                    # Remove old PNGs before regenerating
                    prefix = f"{workspace}_{report_name}_page"
                    for old_png in wiki_dir.glob(f"{prefix}*.png"):
                        old_png.unlink()
                        print(f"  Removed old PNG: {old_png}")

                    # Convert PDF to PNGs
                    png_list = pdf_to_pngs(pdf_path, workspace, report_name, wiki_dir)

                    # Delete PDF from main repo after conversion
                    if pdf_path.exists():
                        pdf_path.unlink()
                        print(f"  Deleted PDF: {pdf_path}")

                    # Parse report data
                    pages = get_pages(report_folder) if report_folder.exists() else []
                    if tables_folder.exists():
                        all_columns, all_measures, sources, dax_tables = get_tables_data(tables_folder)
                    else:
                        all_columns, all_measures, sources, dax_tables = [], [], [], []

                    # Generate .md
                    generate_doc(
                        workspace=workspace,
                        report_name=report_name,
                        wiki_dir=wiki_dir,
                        png_list=png_list,
                        pages=pages,
                        all_columns=all_columns,
                        all_measures=all_measures,
                        sources=sources,
                        dax_tables=dax_tables,
                        existing_report_desc=existing_report_desc,
                        existing_page_descs=existing_page_descs,
                        existing_users=existing_users,
                    )

                elif status == "DELETED":
                    delete_report_from_wiki(workspace, report_name, wiki_dir)

                mark_processed(processed_path, date, report_key)
                processed = read_processed(processed_path)
                any_processed = True

            except Exception as e:
                print(f"  ERROR processing {report_key}: {e}")
                continue

    if any_processed:
        update_home_md(wiki_dir)
        print("\nHome.md updated.")
    else:
        print("\nNothing new to process.")


if __name__ == "__main__":
    main()
