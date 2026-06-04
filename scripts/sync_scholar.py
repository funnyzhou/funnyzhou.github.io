#!/usr/bin/env python3
"""Sync publications.json from Google Scholar profile."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

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


def parse_year(cell) -> int | None:
    if not cell:
        return None
    text = cell.get_text(strip=True)
    if text.isdigit():
        return int(text)
    return None


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
        year = parse_year(row.select_one("td.gsc_a_y"))

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
    session.headers.update(HEADERS)
    all_pubs: list[dict] = []
    cstart = 0
    pagesize = 100

    while True:
        params = {
            "user": user_id,
            "hl": "en",
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

    write_output(publications)
    print(f"Synced {len(publications)} publications -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
