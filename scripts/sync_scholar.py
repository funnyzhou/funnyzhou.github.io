#!/usr/bin/env python3
"""Sync publications.json from Google Scholar profile."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "publications.json"
USER_ID = "aJnvh8gAAAAJ"
SCHOLAR_URL = f"https://scholar.google.com/citations?user={USER_ID}&hl=en"
HIGHLIGHT_NAMES = [
    "Hong-Yu Zhou",
    "Hong-Yu ZHOU",
    "H.-Y. Zhou",
    "H. Y. Zhou",
    "Hongyu Zhou",
    "HY Zhou",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def highlight_authors(authors: str) -> str:
    html = authors
    for name in sorted(HIGHLIGHT_NAMES, key=len, reverse=True):
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        html = pattern.sub(lambda m: f"<b>{m.group(0)}</b>", html)
    return html


def parse_year(cell, venue: str = "") -> int | None:
    if cell:
        text = cell.get_text(strip=True)
        if text.isdigit():
            return int(text)
    match = re.search(r"\b(20\d{2})\b", venue)
    return int(match.group(1)) if match else None


def parse_publications(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.gsc_a_tr")
    publications = []

    for row in rows:
        title_el = row.select_one("a.gsc_a_at")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        url = title_el.get("href", "")
        if url and url.startswith("/"):
            url = "https://scholar.google.com" + url

        grays = row.select("div.gs_gray")
        authors = grays[0].get_text(strip=True) if grays else ""
        venue = grays[1].get_text(strip=True) if len(grays) > 1 else ""
        year = parse_year(row.select_one("td.gsc_a_y"), venue)

        publications.append(
            {
                "title": title,
                "authors_html": highlight_authors(authors),
                "venue": venue,
                "year": year,
                "url": url or None,
            }
        )

    return publications


def fetch_all_publications(user_id: str) -> list[dict]:
    session = requests.Session()
    session.trust_env = False  # ignore system proxy (often blocks Scholar)
    session.headers.update(HEADERS)
    all_pubs: list[dict] = []
    cstart = 0
    pagesize = 100

    while True:
        params = {
            "user": user_id,
            "hl": "en",
            "view_op": "list_works",
            "sortby": "pubdate",
            "cstart": cstart,
            "pagesize": pagesize,
        }
        resp = session.get(
            "https://scholar.google.com/citations",
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        batch = parse_publications(resp.text)
        if not batch:
            break
        all_pubs.extend(batch)
        if len(batch) < pagesize:
            break
        cstart += pagesize

    return all_pubs


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def is_key_author(authors_html: str) -> bool:
    """Return True if HY Zhou appears as 1st, 2nd, or last author."""
    # Remove trailing "et al." and split
    text = strip_tags(authors_html)
    text = re.sub(r",?\s*et al\.?\s*$", "", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return False
    key_positions = {0, 1, len(parts) - 1}
    for i, part in enumerate(parts):
        if i in key_positions:
            for name in HIGHLIGHT_NAMES:
                if name.lower() in part.lower():
                    return True
    return False


def fetch_abstract(title: str) -> str | None:
    """Query Semantic Scholar for the abstract of a paper by title."""
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={quote(title)}&fields=abstract&limit=1"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        papers = data.get("data", [])
        if papers and papers[0].get("abstract"):
            return papers[0]["abstract"]
    except Exception as exc:
        print(f"  Semantic Scholar lookup failed for '{title[:50]}': {exc}", file=sys.stderr)
    return None


def enrich_abstracts(publications: list[dict], existing: dict | None) -> None:
    """Add abstract + is_highlight fields; reuse cached abstracts where available."""
    existing_map: dict[str, str] = {}
    if existing:
        for pub in existing.get("publications", []):
            if pub.get("abstract"):
                existing_map[pub["title"]] = pub["abstract"]

    highlights = [p for p in publications if is_key_author(p["authors_html"])]
    # Only fetch for the 10 most recent highlights (covers the 5 shown + buffer)
    for pub in publications:
        pub["is_highlight"] = is_key_author(pub["authors_html"])

    recent_highlights = [p for p in publications if p["is_highlight"]][:10]
    for pub in recent_highlights:
        if pub["title"] in existing_map:
            pub["abstract"] = existing_map[pub["title"]]
        else:
            print(f"  Fetching abstract: {pub['title'][:60]}…")
            abstract = fetch_abstract(pub["title"])
            pub["abstract"] = abstract
            time.sleep(0.5)  # be polite to Semantic Scholar


def load_existing() -> dict | None:
    if OUTPUT.exists():
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    return None


def write_output(publications: list[dict]) -> None:
    payload = {
        "updated": str(date.today()),
        "scholar_user_id": USER_ID,
        "scholar_url": SCHOLAR_URL,
        "highlight_names": HIGHLIGHT_NAMES,
        "publications": publications,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    try:
        publications = fetch_all_publications(USER_ID)
    except Exception as exc:
        print(f"Failed to fetch Google Scholar: {exc}", file=sys.stderr)
        if OUTPUT.exists():
            print("Keeping existing publications.json unchanged.", file=sys.stderr)
            return 1
        raise

    if not publications:
        print("No publications parsed from Google Scholar.", file=sys.stderr)
        existing = load_existing()
        if existing:
            print("Keeping existing publications.json unchanged.", file=sys.stderr)
            return 1
        return 1

    existing = load_existing()
    enrich_abstracts(publications, existing)
    write_output(publications)
    n_highlights = sum(1 for p in publications if p.get("is_highlight"))
    print(f"Synced {len(publications)} publications ({n_highlights} highlights) -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
