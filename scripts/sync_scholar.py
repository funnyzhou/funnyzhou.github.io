#!/usr/bin/env python3
"""Sync publications.json from Google Scholar profile via scholarly."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

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


def highlight_authors(authors: str) -> str:
    html = authors
    for name in sorted(HIGHLIGHT_NAMES, key=len, reverse=True):
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        html = pattern.sub(lambda m: f"<b>{m.group(0)}</b>", html)
    return html


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def is_key_author(authors_html: str) -> bool:
    """Return True if HY Zhou appears as 1st, 2nd, or last author."""
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
    """Query Semantic Scholar for the abstract of a paper by title, with retry on 429."""
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={quote(title)}&fields=abstract&limit=1"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            papers = resp.json().get("data", [])
            if papers and papers[0].get("abstract"):
                return papers[0]["abstract"]
            return None
        except Exception as exc:
            print(f"  abstract lookup failed for '{title[:50]}': {exc}", file=sys.stderr)
            return None
    return None


def enrich_abstracts(publications: list[dict], existing: dict | None) -> None:
    """Add abstract + is_highlight fields; reuse cached abstracts where available."""
    existing_map: dict[str, str] = {}
    if existing:
        for pub in existing.get("publications", []):
            if pub.get("abstract"):
                existing_map[pub["title"]] = pub["abstract"]

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
            time.sleep(3)


def fetch_publications_serpapi(api_key: str) -> list[dict]:
    """Fetch all publications via SerpAPI Google Scholar Author endpoint."""
    publications = []
    start = 0
    page_size = 100

    while True:
        params = {
            "engine": "google_scholar_author",
            "author_id": USER_ID,
            "api_key": api_key,
            "sort": "pubdate",
            "num": page_size,
            "start": start,
            "hl": "en",
        }
        resp = requests.get("https://serpapi.com/search", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        articles = data.get("articles", [])
        if not articles:
            break

        for article in articles:
            title = article.get("title", "")
            if not title:
                continue
            authors_raw = article.get("authors", "")
            venue = article.get("publication", "") or ""
            # publication field often looks like "Journal Name, 2024"
            # extract year from it if not present separately
            year = article.get("year")
            if not year:
                m = re.search(r"\b(20\d{2})\b", venue)
                year = int(m.group(1)) if m else None
            else:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None

            pub_url = article.get("link") or None

            publications.append(
                {
                    "title": title,
                    "authors_html": highlight_authors(authors_raw),
                    "venue": venue,
                    "year": year,
                    "url": pub_url,
                }
            )

        if len(articles) < page_size:
            break
        start += page_size

    publications.sort(key=lambda p: p["year"] or 0, reverse=True)
    return publications


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
    import os
    existing = load_existing()

    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        print("No SERPAPI_KEY set — cannot sync.", file=sys.stderr)
        if existing:
            print("Keeping existing publications.json unchanged.", file=sys.stderr)
            return 0
        return 1

    try:
        publications = fetch_publications_serpapi(api_key)
    except Exception as exc:
        print(f"Failed to fetch Google Scholar: {exc}", file=sys.stderr)
        if existing:
            print("Keeping existing publications.json unchanged.", file=sys.stderr)
            return 0  # soft failure — don't break CI
        return 1

    if not publications:
        print("No publications parsed from Google Scholar.", file=sys.stderr)
        if existing:
            print("Keeping existing publications.json unchanged.", file=sys.stderr)
            return 0
        return 1

    enrich_abstracts(publications, existing)
    write_output(publications)
    n_highlights = sum(1 for p in publications if p.get("is_highlight"))
    print(f"Synced {len(publications)} publications ({n_highlights} highlights) -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
