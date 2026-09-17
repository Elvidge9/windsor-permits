#!/usr/bin/env python3
"""
Export ADU permits to a printable PDF.

Reads output/permits.csv (produced by windsor_permits.py), filters to the
ADU category, sorts newest-first, and writes output/adu_permits.pdf.

Usage:
    python export_adu_pdf.py                 # all ADU permits
    python export_adu_pdf.py --category "New Construction"
    python export_adu_pdf.py --since 2026-01-01
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                Paragraph, Spacer)

BASE = Path(__file__).resolve().parent
CSV_FILE = BASE / "output" / "permits.csv"


def money(v):
    try:
        return "${:,.0f}".format(float(v))
    except (ValueError, TypeError):
        return v or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="ADU",
                    help='category to export (default "ADU"; use "all" for everything)')
    ap.add_argument("--since", default=None, help="only issue_date >= YYYY-MM-DD")
    ap.add_argument("--out", default=None, help="output PDF path")
    args = ap.parse_args()

    if not CSV_FILE.exists():
        raise SystemExit(f"{CSV_FILE} not found -- run windsor_permits.py first")

    with CSV_FILE.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if args.category.lower() != "all":
        rows = [r for r in rows if r.get("category") == args.category]
    if args.since:
        rows = [r for r in rows if r.get("issue_date", "") >= args.since]
    rows.sort(key=lambda r: r.get("issue_date", ""), reverse=True)

    label = args.category if args.category.lower() != "all" else "All"
    out = Path(args.out) if args.out else (
        BASE / "output" / f"{label.lower().replace(' ', '_')}_permits.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Title"], fontSize=16)
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=7.5, leading=9)
    head = ParagraphStyle("h", parent=styles["Normal"], fontSize=8,
                           leading=10, textColor=colors.white,
                           fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(str(out), pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)

    total = sum(float(r["valuation"]) for r in rows if r.get("valuation"))
    story = [
        Paragraph(f"Windsor {label} Permits", title_style),
        Paragraph(
            f"{len(rows)} permits &nbsp;|&nbsp; total valuation {money(total)} "
            f"&nbsp;|&nbsp; generated {datetime.now():%Y-%m-%d}",
            styles["Normal"]),
        Spacer(1, 10),
    ]

    header = ["Issued", "Permit #", "Type", "Category", "Address",
              "Description", "Valuation"]
    data = [[Paragraph(h, head) for h in header]]
    for r in rows:
        data.append([
            Paragraph(r.get("issue_date", ""), cell),
            Paragraph(r.get("permit_number", ""), cell),
            Paragraph(r.get("permit_type", ""), cell),
            Paragraph(r.get("category", ""), cell),
            Paragraph(r.get("project_address", ""), cell),
            Paragraph(r.get("description", ""), cell),
            Paragraph(money(r.get("valuation")), cell),
        ])

    tbl = Table(data, repeatRows=1, colWidths=[
        0.7*inch, 0.8*inch, 0.7*inch, 0.9*inch, 1.9*inch, 3.4*inch, 0.9*inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4fa3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#eef2f8")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d2e0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    doc.build(story)
    print(f"Wrote {out} ({len(rows)} {label} permits)")


if __name__ == "__main__":
    main()
