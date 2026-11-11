"""Content health check: does the live directory match what ACHC lists today?

Runs after the nightly scrape + Supabase write (its own workflow step, so a
content problem is reported as a content problem, never as a failed
scrape). Read-only. Exits 1 if any check fails.

Checks (thresholds are fractions, tuned to tolerate normal noise such as
duplicate-address conflicts and addresses still waiting on a geocode):
  1. care_type_coverage   -- scraped providers with at least one recognized
                             ACHC program. Caught 2026-11-11: 21% had none
                             because the program parser read the wrong text.
  2. stale_published      -- published listings ACHC hasn't listed within
                             the grace window. Caught 2026-11-11: 822.
  3. listed_but_hidden    -- providers ACHC lists today in a shown care type
                             that are NOT visible on the site.
  4. frozen_listings      -- WARNING only: listed by ACHC tonight, but the
                             merge skipped updating them (orphaned duplicates).

Usage:
  python check_content_health.py normalized_dump.json
Env: SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY;
reads only published rows either way).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import timedelta
from typing import Dict, Iterable, List

sys.path.insert(0, os.path.dirname(__file__))
from retire_unseen import GRACE_DAYS, MVP_SERVICE_SLUGS, parse_ts, scrape_reference_time, select_unseen  # noqa: E402

MVP_PROGRAMS = {"Home Care", "Home Health", "Hospice"}
MIN_CARE_TYPE_COVERAGE = float(os.getenv("HEALTH_MIN_CARE_TYPE_COVERAGE", "0.98"))
MAX_STALE_FRACTION = float(os.getenv("HEALTH_MAX_STALE_FRACTION", "0.0"))
MAX_HIDDEN_FRACTION = float(os.getenv("HEALTH_MAX_HIDDEN_FRACTION", "0.03"))


# ---------------------------------------------------------------------------
# Pure checks (unit tested in test_content_health.py)
# ---------------------------------------------------------------------------

def _services(row: dict) -> List[str]:
    s = row.get("services") or []
    return s if isinstance(s, list) else [x.strip() for x in str(s).split(",") if x.strip()]


def check_care_type_coverage(rows: List[dict], minimum: float = MIN_CARE_TYPE_COVERAGE) -> Dict:
    with_type = sum(1 for r in rows if _services(r))
    frac = with_type / len(rows) if rows else 0.0
    return {"name": "care_type_coverage", "ok": bool(rows) and frac >= minimum,
            "detail": f"{with_type}/{len(rows)} scraped providers have an ACHC program ({frac:.1%}; need {minimum:.0%})"}


def check_stale_published(published: List[dict], reference, seen_ids: Iterable[str] = (),
                          grace_days: float = GRACE_DAYS, maximum: float = MAX_STALE_FRACTION) -> Dict:
    """Published listings ACHC didn't list tonight and hasn't seen within the grace window."""
    stale = select_unseen(published, reference, seen_ids, grace_days)
    frac = len(stale) / len(published) if published else 0.0
    cutoff = reference - timedelta(days=grace_days)
    return {"name": "stale_published", "ok": frac <= maximum,
            "detail": f"{len(stale)} published listings not in ACHC's current list and not seen since "
                      f"{cutoff.date()} (allowed {maximum:.0%})",
            "count": len(stale)}


def check_frozen_listings(published: List[dict], reference, seen_ids: Iterable[str],
                          grace_days: float = GRACE_DAYS) -> Dict:
    """WARNING ONLY: ACHC lists them tonight, but their row hasn't updated within the grace window.

    Cause as of 2026-11-11: the merge skips ~230 rows a night on orphaned
    duplicate-row conflicts, freezing the real listing's data. Reported,
    not failed, until those duplicates are cleaned up -- a check that fails
    every night for a known issue just teaches people to ignore it.
    """
    seen = {str(i) for i in seen_ids if i}
    cutoff = reference - timedelta(days=grace_days)
    frozen = [p for p in published if p.get("achc_source_id") and str(p["achc_source_id"]) in seen
              and (parse_ts(p.get("source_last_seen_at")) or cutoff - timedelta(days=1)) < cutoff]
    return {"name": "frozen_listings", "ok": True, "warn": bool(frozen),
            "detail": f"{len(frozen)} listings ACHC still lists but whose data hasn't updated since {cutoff.date()} "
                      f"(merge skipped them; see orphaned-duplicate cleanup)",
            "count": len(frozen), "examples": [p["achc_source_id"] for p in frozen[:10]]}


def check_listed_but_hidden(rows: List[dict], visible_achc_ids: Iterable[str],
                            maximum: float = MAX_HIDDEN_FRACTION) -> Dict:
    visible = set(visible_achc_ids)
    listed = [r for r in rows if MVP_PROGRAMS & set(_services(r)) and r.get("achc_company_id")]
    hidden = [r for r in listed if str(r["achc_company_id"]) not in visible]
    frac = len(hidden) / len(listed) if listed else 0.0
    return {"name": "listed_but_hidden", "ok": bool(listed) and frac <= maximum,
            "detail": f"{len(hidden)}/{len(listed)} providers ACHC lists in a shown care type are not visible "
                      f"({frac:.1%}; allowed {maximum:.0%})",
            "examples": [r["achc_company_id"] for r in hidden[:10]]}


# ---------------------------------------------------------------------------
# Live data (read-only)
# ---------------------------------------------------------------------------

def fetch_published_mvp(base: str, key: str) -> List[dict]:
    slugs = ",".join(MVP_SERVICE_SLUGS)
    url = (f"{base}/rest/v1/locations?select=location_id,achc_source_id,source_last_seen_at,"
           f"location_service_types!inner(service_types!inner(service_type_slug))"
           f"&listing_status=eq.published&accepts_public_display=eq.true"
           f"&location_service_types.service_types.service_type_slug=in.({slugs})&order=location_id")
    rows, start = [], 0
    while True:
        req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}",
                                                   "Range": f"{start}-{start + 999}"})
        page = json.load(urllib.request.urlopen(req, timeout=60))
        rows.extend(page)
        if len(page) < 1000:
            return rows
        start += 1000


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows = json.load(open(argv[1], encoding="utf-8"))
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    if not base or not key:
        print("SUPABASE_URL and a key are required")
        return 2

    published = fetch_published_mvp(base, key)
    reference = scrape_reference_time(rows, {})
    seen_ids = [r.get("achc_company_id") for r in rows]
    results = [
        check_care_type_coverage(rows),
        check_stale_published(published, reference, seen_ids),
        check_frozen_listings(published, reference, seen_ids),
        check_listed_but_hidden(rows, (p["achc_source_id"] for p in published if p.get("achc_source_id"))),
    ]
    print(f"Content health check -- {len(published)} published listings shown on the site")
    for r in results:
        status = "FAIL" if not r["ok"] else ("WARN" if r.get("warn") else "PASS")
        print(f"  [{status}] {r['name']}: {r['detail']}")
        if (not r["ok"] or r.get("warn")) and r.get("examples"):
            print(f"         e.g. ACHC company ids {r['examples']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
