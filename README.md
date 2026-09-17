# Windsor Construction Permit Scraper

Scrapes permit-level data from the City of Windsor's public **Building
Information Reports** (Major Construction Reports, published as PDFs),
filters for ADUs / water & meter work / new construction, and maintains an
incremental master list that re-runs weekly on GitHub Actions. Includes a
browser dashboard and an ADU-only printable PDF export.

Source page:
`https://www.citywindsor.ca/residents/building/building-information/Building-Information-Reports`

## What each file does

- `windsor_permits.py` — the scraper (discover PDFs → download → parse →
  filter → categorize → write CSV/JSON). Incremental via `state.json`.
- `export_adu_pdf.py` — reads the CSV and writes a printable
  `output/adu_permits.pdf` (default ADU; `--category "New Construction"`
  or `--category all` for others).
- `index.html` — GitHub Pages dashboard: sortable table, search, type and
  category filters, summary cards.
- `.github/workflows/scrape.yml` — runs the scraper + PDF export every
  Monday 11:15 UTC, commits results. Has a manual "Run workflow" button
  with a full-rebuild checkbox.
- `.nojekyll` — lets Pages serve the files as-is.

## Categories

`ADU` · `Water-Meter` · `New Construction` · `Other`. Excludes win over
includes, so remodels, renovations, fences, pools, interior alterations,
fit-ups, demolitions, and standalone fire/sprinkler/EV/roof-unit permits
are filtered out even when they contain construction-ish words.

## Known data limitations (verified July 2026)

1. **No contractor/builder name.** Windsor does not publish the contractor
   of record. The `contractor` column and `repeat_builder` flag exist and
   activate automatically once you wire a source into `enrich_contractor()`
   (FOI export, per-address lookup, or a purchased feed). Until then,
   repeat-builder analysis is done by eye via clustered addresses.
2. **PDF text-layer truncation.** Windsor's PDFs sometimes drop the first
   line of a permit row, removing the CONSTRUCT/ERECT verb. The scraper
   includes recovery patterns for the common truncated shapes
   ("DWELLING WITH ATTACHED…", "DETACHED ADDITIONAL…", etc.), but a small
   number of rows whose descriptions collapse to generic text (e.g. a bare
   "SINGLE FAMILY DWELLING AS PER APPROVED PLANS") can't be recovered
   without risking false positives, so they remain missed.
3. **$250k floor.** The Major Construction Report only lists permits over
   $250,000, so small standalone meter/service permits usually won't
   appear in any public report.
4. **Enwin publishes no permit data.** Water/meter work is only captured
   when it's part of a city permit whose description mentions it.

## Outputs (in `output/`)

- `permits.csv` — newest-first, columns: permit_number, issue_date,
  permit_type, category, project_address, description, valuation,
  contractor, builder_permit_count, repeat_builder, source_report
- `permits_raw.json` — full raw dump
- `adu_permits.pdf` — printable ADU list

## Run locally (optional)

```bash
pip install -r requirements.txt
python windsor_permits.py          # or --full / --dry-run
python export_adu_pdf.py           # ADU PDF
```
