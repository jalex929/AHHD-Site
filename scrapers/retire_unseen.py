"""Hide published listings that ACHC no longer lists, pending human review.

Why this exists (traced 2026-11-11): the only "this provider disappeared"
check used to live in the Google Sheets Apps Script (rows not in a pull were
marked possibly_inactive), and even then it only opened a review item -- it
never hid the listing. When the Sheets sync was retired (2026-07-27) that
check went with it. Result: 822 listings stayed published after dropping out
of ACHC's accredited list (541 last seen in May 2026); spot checks against
ACHC's accreditation lookup returned no accreditation at all for them.

What it does, after a COMPLETE nightly scrape has been written:
  1. Finds published, publicly displayed locations in the active (MVP) care
     types that are NOT in tonight's scrape (by ACHC accreditation ID) and
     whose source_last_seen_at is older than this scrape minus a grace
     period (default 2 days, so one missed run never hides anyone).
     A listing whose ACHC ID IS in tonight's data is never hidden, even if
     its last-seen date is old: ~230 rows a night are skipped by the merge
     because of orphaned duplicate rows (see
     20260731000100_skip_conflicting_duplicate_locations.sql), which freezes
     the real listing's dates even though ACHC still lists it.
  2. Moves them to listing_status='needs_review' -- hidden from the site,
     nothing deleted -- and opens a review queue item explaining why.
  3. If ACHC lists the provider again later, merge_google_sheets_data
     republishes it automatically (confidence 'verified' -> 'published').

Safety rails (all must pass, or nothing is changed):
  - the scrape was national, complete, not test mode, not row-limited, and
    every requested program returned rows;
  - the number to hide is at most MAX_RETIRE_FRACTION of what's published
    (a sudden mass "disappearance" is far likelier to be a scrape problem
    than real churn -- it gets skipped and logged instead).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

import aiohttp

MVP_SERVICE_SLUGS = ("home-care", "home-health-care", "hospice")
GRACE_DAYS = float(os.getenv("RETIRE_UNSEEN_GRACE_DAYS", "2"))
MAX_RETIRE_FRACTION = float(os.getenv("RETIRE_UNSEEN_MAX_FRACTION", "0.15"))
REVIEW_SUMMARY_PREFIX = "Not in ACHC accredited list"
PAGE_SIZE = 1000
PATCH_BATCH = 150  # keeps location_id=in.(...) URLs well under PostgREST/URL limits


# ---------------------------------------------------------------------------
# Pure decision logic (unit tested in test_retire_unseen.py)
# ---------------------------------------------------------------------------

def parse_ts(value) -> Optional[datetime]:
    """Parse an ISO timestamp; naive values are treated as UTC (the runner's clock)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def run_is_safe_to_retire(run_metadata: dict) -> Tuple[bool, str]:
    """Only a full, national, complete scrape can tell us who is missing."""
    if run_metadata.get("test_mode"):
        return False, "test mode"
    if int(run_metadata.get("limit_locations") or 0) > 0:
        return False, "row-limited run"
    if not run_metadata.get("no_state_filter", False):
        return False, "single-state run"
    if run_metadata.get("scrape_status") != "complete":
        return False, f"scrape_status={run_metadata.get('scrape_status')!r}"
    zero = run_metadata.get("programs_with_zero_rows") or []
    if zero:
        return False, f"programs returned 0 rows: {zero}"
    return True, "ok"


def scrape_reference_time(normalized_rows: Iterable[dict], run_metadata: dict) -> Optional[datetime]:
    """Earliest last_seen stamped on this scrape's rows (falls back to the scrape start).

    Uses the rows themselves rather than 'now' so a run resumed from a
    checkpoint is judged against when the data was actually scraped.
    """
    stamps = [parse_ts(r.get("last_seen")) for r in normalized_rows]
    stamps = [s for s in stamps if s]
    if stamps:
        return min(stamps)
    return parse_ts(run_metadata.get("started_at_utc"))


def select_unseen(locations: Iterable[dict], reference: datetime, seen_ids: Iterable[str] = (),
                  grace_days: float = GRACE_DAYS) -> List[dict]:
    """Locations ACHC didn't list tonight AND last seen before (reference - grace).

    Listings with no ACHC ID (orphaned duplicates from the pre-2026-07-27
    pipeline) are judged on their last-seen date alone.
    """
    seen = {str(i) for i in seen_ids if i}
    cutoff = reference - timedelta(days=grace_days)
    out = []
    for loc in locations:
        if loc.get("achc_source_id") and str(loc["achc_source_id"]) in seen:
            continue
        last = parse_ts(loc.get("source_last_seen_at"))
        if last is None or last < cutoff:
            out.append(loc)
    return out


def within_cap(n_to_retire: int, n_published: int, max_fraction: float = MAX_RETIRE_FRACTION) -> bool:
    if n_to_retire == 0:
        return True
    if n_published <= 0:
        return False
    return n_to_retire / n_published <= max_fraction


def review_summary(loc: dict) -> str:
    name = (loc.get("organization") or {}).get("organization_name") or loc.get("location_name") or "[unknown]"
    seen = parse_ts(loc.get("source_last_seen_at"))
    when = seen.date().isoformat() if seen else "never recorded"
    return f"{REVIEW_SUMMARY_PREFIX} (last seen {when}): {name}. Hidden until verified."


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def _headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def fetch_published_mvp_locations(session: aiohttp.ClientSession, base: str, key: str) -> List[dict]:
    slugs = ",".join(MVP_SERVICE_SLUGS)
    url = (
        f"{base}/rest/v1/locations?select=location_id,location_name,achc_source_id,source_last_seen_at,"
        f"organization:organizations(organization_name),"
        f"location_service_types!inner(service_types!inner(service_type_slug))"
        f"&listing_status=eq.published&accepts_public_display=eq.true"
        f"&location_service_types.service_types.service_type_slug=in.({slugs})"
        f"&order=location_id"
    )
    rows: List[dict] = []
    start = 0
    while True:
        headers = {**_headers(key), "Range": f"{start}-{start + PAGE_SIZE - 1}"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status not in (200, 206):
                raise Exception(f"fetch published locations failed: HTTP {resp.status} {(await resp.text())[:200]}")
            page = await resp.json()
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


async def _open_review_location_ids(session, base, key, location_ids: List[str]) -> set:
    found = set()
    for i in range(0, len(location_ids), PATCH_BATCH):
        chunk = ",".join(location_ids[i:i + PATCH_BATCH])
        url = (
            f"{base}/rest/v1/review_queue_items?select=location_id"
            f"&location_id=in.({chunk})&review_status=eq.open"
            f"&issue_summary=like.{REVIEW_SUMMARY_PREFIX.replace(' ', '%20')}*"
        )
        async with session.get(url, headers=_headers(key), timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise Exception(f"review lookup failed: HTTP {resp.status} {(await resp.text())[:200]}")
            found.update(r["location_id"] for r in await resp.json())
    return found


async def retire_unseen_locations(normalized_rows: List[dict], run_metadata: dict, dry_run: bool = False) -> dict:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    result = {"retired": 0, "reviews_opened": 0, "skipped_reason": "", "dry_run": dry_run}
    if not base or not key:
        result["skipped_reason"] = "Supabase credentials not set"
        return result

    safe, why = run_is_safe_to_retire(run_metadata)
    if not safe:
        result["skipped_reason"] = why
        print(f"Retire-unseen skipped: {why}")
        return result

    reference = scrape_reference_time(normalized_rows, run_metadata)
    if reference is None:
        result["skipped_reason"] = "no scrape timestamp"
        return result

    async with aiohttp.ClientSession() as session:
        published = await fetch_published_mvp_locations(session, base, key)
        seen_ids = [r.get("achc_company_id") for r in normalized_rows]
        unseen = select_unseen(published, reference, seen_ids)
        result.update(published_before=len(published), candidates=len(unseen), reference=reference.isoformat())
        print(f"Retire-unseen: {len(unseen)} of {len(published)} published listings not in tonight's ACHC data, not seen since "
              f"{(reference - timedelta(days=GRACE_DAYS)).date()} (grace {GRACE_DAYS:g} days)")

        if not within_cap(len(unseen), len(published)):
            result["skipped_reason"] = (f"{len(unseen)}/{len(published)} exceeds the "
                                        f"{MAX_RETIRE_FRACTION:.0%} safety cap -- likely a scrape problem, nothing hidden")
            print(f"Retire-unseen skipped: {result['skipped_reason']}")
            return result
        if not unseen or dry_run:
            if dry_run:
                print("Retire-unseen dry run: no changes written")
            return result

        ids = [loc["location_id"] for loc in unseen]
        already_open = await _open_review_location_ids(session, base, key, ids)
        for i in range(0, len(ids), PATCH_BATCH):
            chunk = ids[i:i + PATCH_BATCH]
            url = f"{base}/rest/v1/locations?location_id=in.({','.join(chunk)})&listing_status=eq.published"
            async with session.patch(url, json={"listing_status": "needs_review"},
                                     headers={**_headers(key), "Prefer": "return=minimal"},
                                     timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status not in (200, 204):
                    raise Exception(f"retire patch failed: HTTP {resp.status} {(await resp.text())[:200]}")
            result["retired"] += len(chunk)

        now = datetime.now(timezone.utc).isoformat()
        items = [{
            "location_id": loc["location_id"], "review_type": "other", "severity": "medium",
            "review_status": "open", "issue_summary": review_summary(loc), "opened_at": now,
        } for loc in unseen if loc["location_id"] not in already_open]
        for i in range(0, len(items), PATCH_BATCH):
            async with session.post(f"{base}/rest/v1/review_queue_items", json=items[i:i + PATCH_BATCH],
                                    headers={**_headers(key), "Prefer": "return=minimal"},
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status not in (200, 201, 204):
                    raise Exception(f"review insert failed: HTTP {resp.status} {(await resp.text())[:200]}")
            result["reviews_opened"] += len(items[i:i + PATCH_BATCH])

    print(f"Retire-unseen: hid {result['retired']} listings (needs_review), opened {result['reviews_opened']} review items")
    return result
