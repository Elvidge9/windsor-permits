#!/usr/bin/env python3
"""
Windsor Construction Permit Scraper
===================================
Pulls permit-level data from the City of Windsor's public
"Building Information Reports" page (Major Construction Reports, published
as PDFs):

    https://www.citywindsor.ca/residents/building/building-information/Building-Information-Reports

Filters for: ADUs / additional dwelling units, water service & meter work,
and new residential/commercial/industrial/institutional construction.
Skips: remodels, renovations, fences, pools, and interior-only alterations.

For each matched permit it captures: permit number, issue date, permit
type, category (ADU / Water-Meter / New Construction), project address,
description, and construction value. Contractor is left blank because
Windsor does not publish it (see README); the repeat-builder flag logic
activates automatically once contractor values are supplied.

Outputs (in output/):
    permits_raw.json  - full raw dump of every matched permit
    permits.csv       - sorted by issue date, newest first, with
                        builder_permit_count + repeat_builder columns

Incremental: state.json remembers processed reports and seen permit
numbers so weekly re-runs only add new permits.

Usage:
    python windsor_permits.py            # incremental run
    python windsor_permits.py --full     # ignore state, reprocess all
    python windsor_permits.py --dry-run  # parse + report, write nothing
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber is required: pip install pdfplumber")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

REPORTS_PAGE = (
    "https://www.citywindsor.ca/residents/building/"
    "building-information/Building-Information-Reports"
)
USER_AGENT = (
    "WindsorPermitResearch/1.0 (personal research; contact: you@example.com)"
)
RATE_LIMIT_SECONDS = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF = 10

# Descriptions must match at least one pattern (case-insensitive). The first
# category whose patterns match becomes the permit's `category`.
CATEGORY_PATTERNS = {
    "ADU": [
        r"\bADU\b",
        r"\bADUS\b",
        r"ADDITIONAL DWELLING UNIT",
        r"SECONDARY\s*/?\s*ADDITIONAL",
        r"SECONDARY SUITE",
        r"DETACHED ADDITIONAL",   # "...DETACHED ADDITIONAL DWELLING UNIT"
    ],
    "Water-Meter": [
        r"\bWATER SERVICE\b",
        r"\bWATER METER\b",
        r"\bMETER PIT\b",
        r"\bMETER INSTALLATION\b",
        r"\bWATERMAIN\b",
    ],
    "New Construction": [
        # CONSTRUCT/ERECT paired with a building noun (avoids "construct
        # partition wall" style interior work).
        r"\bCONSTRUCT\b.*\b(DWELLING|TOWNHOUSE|TOWNHOME|DUPLEX|TRIPLEX|"
        r"APARTMENT|MULTIPLE DWELLING|SEMI-?DETACHED|SINGLE (UNIT|FAMILY))\b",
        r"\bNEW\b.*\bDWELLING\b",
        r"\bERECT\b",
        r"\bCONSTRUCT\b.*\b(BUILDING|WAREHOUSE|STORE|PLAZA|CENTRE|CENTER|"
        r"FACILITY|OFFICE|RESTAURANT|HOTEL|SCHOOL|CHURCH|CLINIC|"
        r"CARE FACILITY|FIELD HOUSE|GARAGE)\b",
        # --- Truncated-row recovery -------------------------------------
        # Windsor's PDF text layer frequently drops the first line of a row,
        # so "CONSTRUCT 2 STOREY SINGLE FAMILY DWELLING WITH ATTACHED
        # GARAGE..." arrives as "( ) DWELLING WITH ATTACHED GARAGE...".
        # These shapes recover those rows. They're deliberately anchored to
        # "DWELLING WITH <build feature>" so interior-alteration permits
        # (caught by EXCLUDE below) don't slip through.
        r"DWELLING WITH (ATTACHED|FINISHED|UNFINISHED|FRONT|REAR|GRADE|TWO|"
        r"\(?\d)",
        r"DETACHED DWELLING WITH",
        r"\bDWELLING UNIT \(\d+ UNITS?\)",
        r"FAMILY DWELLING,",
        # leading "WITH ATTACHED GARAGE, GRADE ENTRANCE, FINISHED LOWER
        # LEVEL" (whole first line incl. the word DWELLING was dropped).
        # Requires two build features so it can't match generic text.
        r"\bWITH ATTACHED GARAGE, (GRADE|FINISHED|UNFINISHED|COVERED)",
    ],
}
INCLUDE_PATTERNS = [p for pats in CATEGORY_PATTERNS.values() for p in pats]

# Excludes win over includes.
EXCLUDE_PATTERNS = [
    r"\bREMODEL",
    r"\bRENOVAT",
    r"\bFENCE\b",
    r"\bFENCING\b",
    r"\bPOOL\b",
    r"\bHOT TUB\b",
    r"INTERIOR ALTERATIONS",
    r"INTERIOR & EXTERIOR ALTERATIONS",
    r"INTERIOR AND EXTERIOR ALTERATIONS",
    r"INTERIOR FIT",
    r"INTERIOR FINISHING TO (EXISTING|VACANT)",
    r"EXTERIOR ALTERATIONS",
    r"EXTERIOR BRICK",
    r"BRICK VENEER",
    r"FACADE",
    r"STRUCTURAL SLAB REPAIR",
    r"CONCRETE REPAIR",
    r"\bDEMOLI",
    r"REPLACE EXISTING ROOFTOP",
    r"REPLACE \(?\d?\)? ?ROOFTOP",
    r"FIRE ALARM",
    r"FIRE PROTECTION",
    r"FIRE SUPPRESSION",
    r"SPRINKLER",
    r"EV CHARGER",
    r"OIL TANK",
    r"LED AND STATIC",
    r"ELECTRONIC LED",
]

INCLUDE_RE = [re.compile(p, re.IGNORECASE) for p in INCLUDE_PATTERNS]
EXCLUDE_RE = [re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATTERNS]
CATEGORY_RE = {cat: [re.compile(p, re.IGNORECASE) for p in pats]
               for cat, pats in CATEGORY_PATTERNS.items()}

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
OUTPUT_DIR = BASE_DIR / "output"
RAW_JSON = OUTPUT_DIR / "permits_raw.json"
CSV_FILE = OUTPUT_DIR / "permits.csv"
PDF_CACHE = BASE_DIR / "pdf_cache"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("windsor-permits")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Permit:
    permit_number: str
    issue_date: str
    permit_type: str
    category: str
    project_address: str
    description: str
    valuation: float
    contractor: str = ""
    source_report: str = ""
    scraped_at: str = ""


# --------------------------------------------------------------------------
# HTTP with rate limiting
# --------------------------------------------------------------------------

class PoliteSession:
    def __init__(self, delay: float = RATE_LIMIT_SECONDS):
        self.delay = delay
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get(self, url: str, **kwargs) -> requests.Response:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        backoff = RETRY_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._last = time.monotonic()
                resp = self.session.get(url, timeout=60, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise
                log.warning("Request failed (%s), retry %d/%d in %ds",
                            exc, attempt, MAX_RETRIES, backoff)
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_permit_numbers": [], "processed_reports": {}, "last_run": None}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Discover report PDFs
# --------------------------------------------------------------------------

def discover_report_pdfs(http: PoliteSession) -> list[str]:
    log.info("Fetching report index: %s", REPORTS_PAGE)
    resp = http.get(REPORTS_PAGE)
    soup = BeautifulSoup(resp.text, "html.parser")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() and "building-information" in href.lower():
            full = urljoin(REPORTS_PAGE, href)
            if full not in seen:
                seen.add(full)
                urls.append(full)
    log.info("Found %d report PDF(s)", len(urls))
    return urls


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------

ROW_RE = re.compile(
    r"(?P<permit>20\d{2}\s?\d{6})\s+CPBC\s+"
    r"(?P<middle>.*?)"
    r"\$(?P<value>[\d,]+\.\d{2})\s+"
    r"(?P<ptype>Residential|Commercial|Industrial|Institutional)",
    re.DOTALL,
)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
NOISE_RE = re.compile(
    r"(Permit number\s+Municipal address\s+Issued Date.*?Type of Construction"
    r"|Page \d+ of \d+"
    r"|Value of\s*\n?\s*construction)",
    re.DOTALL,
)


def clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_report_pdf(pdf_bytes: bytes, source_url: str) -> list[Permit]:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    text = NOISE_RE.sub(" ", "\n".join(parts))

    permits, now = [], datetime.now(timezone.utc).isoformat()
    for m in ROW_RE.finditer(text):
        middle = m.group("middle")
        dm = DATE_RE.search(middle)
        if not dm:
            continue
        permits.append(Permit(
            permit_number=clean_ws(m.group("permit")),
            issue_date=dm.group(1),
            permit_type=m.group("ptype"),
            category="",
            project_address=clean_ws(middle[:dm.start()]),
            description=clean_ws(middle[dm.end():]),
            valuation=float(m.group("value").replace(",", "")),
            source_report=source_url,
            scraped_at=now,
        ))
    return permits


# --------------------------------------------------------------------------
# Filter + categorize
# --------------------------------------------------------------------------

def wanted(p: Permit) -> bool:
    hay = f"{p.description} {p.project_address}"
    if any(rx.search(hay) for rx in EXCLUDE_RE):
        return False
    return any(rx.search(hay) for rx in INCLUDE_RE)


def categorize(p: Permit) -> str:
    hay = f"{p.description} {p.project_address}"
    for cat, regexes in CATEGORY_RE.items():
        if any(rx.search(hay) for rx in regexes):
            return cat
    return "Other"


def enrich_contractor(p: Permit) -> Permit:
    """Windsor doesn't publish the contractor of record. Wire a source in
    here (FOI export, per-address lookup, purchased feed) and the repeat-
    builder flags below light up automatically."""
    return p


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

def write_outputs(all_permits: list[Permit]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    RAW_JSON.write_text(json.dumps([asdict(p) for p in all_permits], indent=2))

    counts = Counter(p.contractor for p in all_permits if p.contractor)
    rows = sorted(all_permits, key=lambda p: (p.issue_date, p.permit_number),
                  reverse=True)
    with CSV_FILE.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["permit_number", "issue_date", "permit_type", "category",
                    "project_address", "description", "valuation", "contractor",
                    "builder_permit_count", "repeat_builder", "source_report"])
        for p in rows:
            n = counts.get(p.contractor, 0) if p.contractor else 0
            w.writerow([p.permit_number, p.issue_date, p.permit_type, p.category,
                        p.project_address, p.description, f"{p.valuation:.2f}",
                        p.contractor, n if n else "", "YES" if n > 1 else "",
                        p.source_report])
    log.info("Wrote %d permits -> %s + %s",
             len(all_permits), RAW_JSON.name, CSV_FILE.name)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="ignore saved state and reprocess every report")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but write no files")
    args = ap.parse_args()

    state = (load_state() if not args.full
             else {"seen_permit_numbers": [], "processed_reports": {},
                   "last_run": None})
    seen = set(state["seen_permit_numbers"])

    http = PoliteSession()
    PDF_CACHE.mkdir(exist_ok=True)

    existing: list[Permit] = []
    if RAW_JSON.exists() and not args.full:
        existing = [Permit(**d) for d in json.loads(RAW_JSON.read_text())]

    new_permits: list[Permit] = []
    for url in discover_report_pdfs(http):
        if state["processed_reports"].get(url) and not args.full:
            log.info("Skipping processed report: %s", url.split("/")[-1])
            continue
        log.info("Downloading %s", url.split("/")[-1])
        try:
            pdf_bytes = http.get(url).content
        except requests.RequestException as exc:
            log.error("Failed to download %s: %s", url, exc)
            continue

        sha = hashlib.sha256(pdf_bytes).hexdigest()
        (PDF_CACHE / f"{sha[:16]}.pdf").write_bytes(pdf_bytes)

        rows = parse_report_pdf(pdf_bytes, url)
        matched = [enrich_contractor(p) for p in rows if wanted(p)]
        for p in matched:
            p.category = categorize(p)
        fresh = [p for p in matched if p.permit_number not in seen]
        seen.update(p.permit_number for p in fresh)
        new_permits.extend(fresh)

        state["processed_reports"][url] = {
            "sha256": sha, "parsed_rows": len(rows),
            "matched_rows": len(matched),
            "last_fetched": datetime.now(timezone.utc).isoformat(),
        }
        log.info("  parsed %d, matched %d, new %d",
                 len(rows), len(matched), len(fresh))
        if not rows:
            log.warning("  no permit rows -- likely a summary dashboard PDF")

    all_permits = existing + new_permits
    log.info("Summary: %d new, %d total", len(new_permits), len(all_permits))

    if args.dry_run:
        for p in sorted(new_permits, key=lambda p: p.issue_date, reverse=True):
            print(f"{p.issue_date}  {p.category:16s}  ${p.valuation:>12,.0f}  "
                  f"{p.project_address}")
        return 0

    write_outputs(all_permits)
    state["seen_permit_numbers"] = sorted(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
