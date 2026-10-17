"""Scrape every Bellhaven community from the public website.

Community links come from the homepage AND every page of the directory,
then de-duplicated. One community (Meadows of Findlay) only appears on the
homepage, so the homepage must be read too.
"""
import json
import os
import re

import requests
from bs4 import BeautifulSoup

SITE = "https://analyst-assessment-production.up.railway.app"
EXPECTED_COUNT = 35  # the homepage says 35; the directory says 34 plus one homepage-only community
COMMUNITIES_FILE = "data/communities.json"

SLUG_RE = re.compile(r"^/communities/([^/?#]+)$")
CITY_LINE_RE = re.compile(r"^(.+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")
PAGE_OF_RE = re.compile(r"Page\s+\d+\s+of\s+(\d+)")


def _soup(path):
    r = requests.get(SITE + path, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def collect_slugs():
    """Return every community slug from the homepage and all directory pages."""
    slugs = set()

    # Homepage: the "new this year" banner links to a community not in the directory.
    for a in _soup("/").find_all("a", href=True):
        m = SLUG_RE.match(a["href"])
        if m:
            slugs.add(m.group(1))

    # Directory: follow "Page X of Y" so we never hard-code the page count.
    first = _soup("/communities")
    slugs |= _slugs_on(first)
    total_pages = int(PAGE_OF_RE.search(first.get_text(" ")).group(1))
    for page in range(2, total_pages + 1):
        slugs |= _slugs_on(_soup(f"/communities?page={page}"))
    return slugs


def _slugs_on(soup):
    found = set()
    for a in soup.find_all("a", href=True):
        m = SLUG_RE.match(a["href"])
        if m:
            found.add(m.group(1))
    return found


def parse_detail(slug):
    soup = _soup(f"/communities/{slug}")
    record = {
        "slug": slug,
        "name": soup.find("h1").get_text(strip=True),
        "street": None,
        "city": None,
        "state": None,
        "zip": None,
        "care_offerings": [],
        "administrator": None,
        "phone": None,
    }
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True)
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        if label == "Address":
            # Lines are split by <br>: street first, then "City, ST ZIP".
            lines = [s.strip() for s in dd.stripped_strings]
            if lines:
                record["street"] = lines[0]
            if len(lines) > 1:
                m = CITY_LINE_RE.match(lines[1])
                if m:
                    record["city"], record["state"], record["zip"] = m.groups()
        elif label == "Care Offerings":
            record["care_offerings"] = [b.get_text(strip=True) for b in dd.find_all("span", class_="badge")]
        elif label == "Administrator":
            record["administrator"] = dd.get_text(strip=True)
        elif label == "Phone":
            record["phone"] = dd.get_text(strip=True)
    return record


def scrape_all():
    out = [parse_detail(s) for s in sorted(collect_slugs())]
    incomplete = [c["slug"] for c in out if not (c["street"] and c["zip"])]
    if incomplete:  # a broken scrape would flood the queue with bogus creates and orphans
        raise RuntimeError(f"Scrape incomplete, nothing proposed: {incomplete}")
    return out


if __name__ == "__main__":
    communities = scrape_all()
    os.makedirs(os.path.dirname(COMMUNITIES_FILE), exist_ok=True)
    with open(COMMUNITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(communities, f, indent=2)

    assert len(communities) == EXPECTED_COUNT, f"expected {EXPECTED_COUNT}, got {len(communities)}"
    incomplete = [c["slug"] for c in communities if not (c["street"] and c["zip"] and c["care_offerings"])]
    print(f"Saved {len(communities)} communities to {COMMUNITIES_FILE}")
    print(f"Incomplete records: {incomplete or 'none'}")
