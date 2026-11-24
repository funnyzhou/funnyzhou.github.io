#!/usr/bin/env python3
"""Sync Google Scholar publications and enrich their author lists."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
TRUNCATED_AUTHORS = re.compile(r"\.{3}|…|\bet\s+al\b", re.IGNORECASE)
HIGHLIGHT_AUTHOR = re.compile("|".join(map(re.escape, HIGHLIGHT_NAMES)), re.IGNORECASE)


def highlight_authors(authors: str) -> str:
    return HIGHLIGHT_AUTHOR.sub(lambda m: f"<b>{m.group(0)}</b>", escape(authors))


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def is_key_author(authors_html: str) -> bool:
    """Return True if HY Zhou appears as 1st, 2nd, or last author."""
    text = strip_tags(authors_html)
    leading_authors = TRUNCATED_AUTHORS.split(text, maxsplit=1)[0]
    parts = [p.strip() for p in leading_authors.split(",") if p.strip()]
    key_authors = parts[:2]
    if not TRUNCATED_AUTHORS.search(text):
        key_authors += parts[-1:]
    return any(HIGHLIGHT_AUTHOR.fullmatch(name) for name in key_authors)


def tag_highlights(publications: list[dict]) -> None:
    for pub in publications:
        pub["is_highlight"] = is_key_author(pub["authors_html"])


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", unescape(strip_tags(text))).casefold()


def crossref_work(pub: dict, session: requests.Session) -> dict:
    venue = normalize(pub["venue"])
    if not venue or re.search(r"arxiv|preprint", pub["venue"], re.IGNORECASE):
        return {}
    response = session.get(
        "https://api.crossref.org/works",
        params={
            "query.bibliographic": f"{pub['title']} {pub['venue']}",
            "rows": 5,
            "select": "title,container-title,author,DOI,type,published-online,published-print,published",
        },
        timeout=5,
    )
    response.raise_for_status()
    title = normalize(pub["title"])
    matches = [
        work for work in response.json()["message"]["items"]
        if any(normalize(value) == title for value in work.get("title", []))
        and any(
            normalize(container) and venue.startswith(normalize(container))
            for container in work.get("container-title", [])
        )
    ]
    return matches[0] if len(matches) == 1 else {}


def add_crossref_publication_date(pub: dict, work: dict) -> None:
    if work.get("type") not in {"journal-article", "proceedings-article"}:
        return
    for field in ("published-online", "published-print", "published"):
        parts = (work.get(field, {}).get("date-parts") or [[]])[0]
        if len(parts) < 2:
            continue
        try:
            publication_date = date(parts[0], parts[1], parts[2] if len(parts) >= 3 else 1)
        except (TypeError, ValueError):
            continue
        pub["published_date"] = str(publication_date)[:10 if len(parts) == 3 else 7]
        if work.get("DOI"):
            pub["doi"] = work["DOI"]
        return


def crossref_author_names(work: dict) -> str:
    authors = [
        " ".join(filter(None, (author.get("given"), author.get("family"))))
        or author.get("name", "")
        for author in work.get("author", [])
    ]
    return ", ".join(authors) if all(authors) else ""


def scholar_citation_authors(pub: dict, session: requests.Session, api_key: str) -> str:
    citation_ids = parse_qs(urlsplit(pub["url"] or "").query).get("citation_for_view")
    if not citation_ids:
        return ""
    response = session.get(
        "https://serpapi.com/search",
        params={
            "engine": "google_scholar_author",
            "view_op": "view_citation",
            "citation_id": citation_ids[0],
            "api_key": api_key,
        },
        timeout=10,
    )
    response.raise_for_status()
    citation = response.json().get("citation", {})
    if normalize(citation.get("title", "")) == normalize(pub["title"]):
        return citation.get("authors") or ""
    return ""


def has_complete_authors(authors: str, current: str) -> bool:
    if not authors.strip() or TRUNCATED_AUTHORS.search(authors):
        return False
    visible, *truncation = TRUNCATED_AUTHORS.split(current, maxsplit=1)
    visible_count = sum(bool(name.strip()) for name in visible.split(","))
    return len(authors.split(",")) >= visible_count + bool(truncation)


def enrich_authors(publications: list[dict], existing: dict | None, api_key: str = "") -> None:
    # Reuse metadata for the same Scholar record and publication version.
    cached = {
        (pub["url"], pub["title"], pub["venue"]): pub
        for pub in (existing or {}).get("publications", [])
        if pub.get("url") and (pub.get("author_source") or pub.get("published_date"))
    }
    with requests.Session() as session:
        for pub in publications:
            previous = cached.get((pub["url"], pub["title"], pub["venue"]))
            if previous:
                for field in ("published_date", "doi"):
                    if field in previous:
                        pub[field] = previous[field]
                if previous.get("author_source"):
                    pub["authors_html"] = previous["authors_html"]
                    pub["author_source"] = previous["author_source"]
                    # Older publications predate the automatic news feed.
                    if pub.get("published_date") or (pub.get("year") or 0) < 2026:
                        continue

            authors = ""
            source = "Crossref"
            try:
                work = crossref_work(pub, session)
                add_crossref_publication_date(pub, work)
                authors = crossref_author_names(work)
            except (requests.RequestException, ValueError, KeyError):
                print(f"Crossref lookup failed: {pub['title']}", file=sys.stderr)

            current = strip_tags(pub["authors_html"])
            incomplete = not current.strip() or TRUNCATED_AUTHORS.search(current)
            if api_key and not pub.get("author_source") and not has_complete_authors(authors, current):
                source = "Google Scholar citation"
                try:
                    authors = scholar_citation_authors(pub, session, api_key)
                except (requests.RequestException, ValueError):
                    print(f"Scholar citation lookup failed: {pub['title']}", file=sys.stderr)

            if has_complete_authors(authors, current):
                pub["authors_html"] = highlight_authors(authors)
                pub["author_source"] = source
            elif incomplete:
                print(f"Complete author list unavailable: {pub['title']}", file=sys.stderr)


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

    enrich_authors(publications, existing, api_key)
    tag_highlights(publications)
    write_output(publications)
    n_highlights = sum(1 for p in publications if p.get("is_highlight"))
    print(f"Synced {len(publications)} publications ({n_highlights} highlights) -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
