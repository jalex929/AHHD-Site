"""Keep the directory in step with ACHC's current accredited list.

Two rules, applied after the nightly scrape has been written (Jay, 2026-11-11):

  1. NOT LISTED by ACHC -> unpublished.
     If ACHC no longer lists a provider, we treat it as no longer accredited:
     listing_status='unpublished' and unpublished_at=now. No review item --
     there's nothing for a person to decide. Nothing is deleted, and if ACHC
     lists the provider again, merge_google_sheets_data republishes it
     automatically (confidence 'verified' -> 'published').

  2. LISTED by ACHC but no recognizable program ("care type unknown") ->
     review queue. The provider is accredited, but we can't tell what it is
     accredited for, so it can't be filed under Home Care / Home Health /
     Hospice. Moved to listing_status='needs_review' with a review item.

Why rule 1 exists (traced 2026-11-11): the only "this provider disappeared"
check lived in the retired Google Sheets script (it never hid anything
either). 822 listings stayed published after leaving ACHC's list; spot
checks against ACHC's accreditation lookup found no accreditation for them.

Who counts as "not listed": published, publicly displayed locations in the
shown care types whose ACHC ID is NOT in tonight's scrape and whose
source_last_seen_at is older than this scrape minus a grace period (default
2 days, so one missed run never removes anyone).

Safety rails:
  - Rule 1 only runs after a national, complete, non-test, non-row-limited
    scrape in which every requested program returned rows.
  - Rule 1 won't unpublish more than MAX_RETIRE_FRACTION of what's published
    at once; rule 2 won't flag more than MAX_UNTAGGED_FRACTION of tonight's
    rows. Above either cap the step changes nothing and logs why (a mass
    change is far likelier to be a scrape or parser problem than real
    churn -- the content health check will fail loudly instead).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

import aiohttp

MVP_SERVICE_SLUGS = ("home-care", "home-health-care", "hospice")
GRACE_DAYS = float(os.getenv("RETIRE_UNSEEN_GRACE_DAYS", "2"))
MAX_RETIRE_FRACTION = float(os.getenv("RETIRE_UNSEEN_MAX_FRACTION", "0.15"))
MAX_UNTAGGED_FRACTION = float(os.getenv("UNTAGGED_MAX_FRACTION", "0.05"))
UNTAGGED_SUMMARY_PREFIX = "Listed by ACHC, care type unknown"
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

    A listing whose ACHC ID is in tonight's data is never selected, even if
    its last-seen date is old (a merge-skipped row can freeze its dates).
    Listings with no ACHC ID are judged on their last-seen date alone.
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


def _services(row: dict) -> List[str]:
    s = row.get("services") or []
    return s if isinstance(s, list) else [x.strip() for x in str(s).split(",") if x.strip()]


def select_untagged(normalized_rows: Iterable[dict]) -> List[str]:
    """ACHC IDs listed tonight with no recognizable program at all."""
    return [str(r["achc_company_id"]) for r in normalized_rows
            if r.get("achc_company_id") and not _services(r)]


def within_cap(n_changes: int, n_total: int, max_fraction: float = MAX_RETIRE_FRACTION) -> bool:
    if n_changes == 0:
        return True
    if n_total <= 0:
        return False
    return n_changes / n_total <= max_fraction


def untagged_summary(name: str) -> str:
    return (f"{UNTAGGED_SUMMARY_PREFIX}: {name or '[unknown]'}. ACHC lists this provider, but no program "
            f"(Home Care / Home Health / Hospice) was found. Confirm the care type before publishing.")


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def _headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _get_all(session: aiohttp.ClientSession, url: str, key: str) -> List[dict]:
    rows: List[dict] = []
    start = 0
    while True:
        headers = {**_headers(key), "Range": f"{start}-{start + PAGE_SIZE - 1}"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status not in (200, 206):
                raise Exception(f"GET failed: HTTP {resp.status} {(await resp.text())[:200]}")
            page = await resp.json()
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


async def fetch_published_mvp_locations(session: aiohttp.ClientSession, base: str, key: str) -> List[dict]:
    slugs = ",".join(MVP_SERVICE_SLUGS)
    return await _get_all(session, (
        f"{base}/rest/v1/locations?select=location_id,location_name,achc_source_id,source_last_seen_at,"
        f"organization:organizations(organization_name),"
        f"location_service_types!inner(service_types!inner(service_type_slug))"
        f"&listing_status=eq.published&accepts_public_display=eq.true"
        f"&location_service_types.service_types.service_type_slug=in.({slugs})"
        f"&order=location_id"), key)


async def _patch_locations(session, base, key, location_ids: List[str], body: dict) -> int:
    changed = 0
    for i in range(0, len(location_ids), PATCH_BATCH):
        chunk = location_ids[i:i + PATCH_BATCH]
        url = f"{base}/rest/v1/locations?location_id=in.({','.join(chunk)})&listing_status=eq.published"
        async with session.patch(url, json=body, headers={**_headers(key), "Prefer": "return=representation"},
                                 timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status not in (200, 204):
                raise Exception(f"patch failed: HTTP {resp.status} {(await resp.text())[:200]}")
            changed += len(await resp.json()) if resp.status == 200 else 0
    return changed


async def _open_review_location_ids(session, base, key, location_ids: List[str], prefix: str) -> set:
    found = set()
    for i in range(0, len(location_ids), PATCH_BATCH):
        chunk = ",".join(location_ids[i:i + PATCH_BATCH])
        url = (f"{base}/rest/v1/review_queue_items?select=location_id"
               f"&location_id=in.({chunk})&review_status=eq.open"
               f"&issue_summary=like.{prefix.replace(' ', '%20').replace(',', '%2C')}*")
        async with session.get(url, headers=_headers(key), timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise Exception(f"review lookup failed: HTTP {resp.status} {(await resp.text())[:200]}")
            found.update(r["location_id"] for r in await resp.json())
    return found


async def _unpublish_unlisted(session, base, key, normalized_rows, run_metadata, dry_run, result) -> None:
    safe, why = run_is_safe_to_retire(run_metadata)
    if not safe:
        result["unlisted_skipped_reason"] = why
        print(f"Unpublish-unlisted skipped: {why}")
        return
    reference = scrape_reference_time(normalized_rows, run_metadata)
    if reference is None:
        result["unlisted_skipped_reason"] = "no scrape timestamp"
        return

    published = await fetch_published_mvp_locations(session, base, key)
    unseen = select_unseen(published, reference, [r.get("achc_company_id") for r in normalized_rows])
    result.update(published_before=len(published), unlisted_candidates=len(unseen))
    print(f"Unpublish-unlisted: {len(unseen)} of {len(published)} published listings not in ACHC's list tonight "
          f"and not seen since {(reference - timedelta(days=GRACE_DAYS)).date()} (grace {GRACE_DAYS:g} days)")
    if not within_cap(len(unseen), len(published), MAX_RETIRE_FRACTION):
        result["unlisted_skipped_reason"] = (f"{len(unseen)}/{len(published)} exceeds the {MAX_RETIRE_FRACTION:.0%} "
                                             f"safety cap -- likely a scrape problem, nothing changed")
        print(f"Unpublish-unlisted skipped: {result['unlisted_skipped_reason']}")
        return
    if dry_run or not unseen:
        return
    now = datetime.now(timezone.utc).isoformat()
    result["unpublished"] = await _patch_locations(
        session, base, key, [l["location_id"] for l in unseen],
        {"listing_status": "unpublished", "unpublished_at": now})


async def _review_untagged(session, base, key, normalized_rows, dry_run, result) -> None:
    ids = select_untagged(normalized_rows)
    result["untagged_listed"] = len(ids)
    if not ids:
        return
    if not within_cap(len(ids), len(normalized_rows), MAX_UNTAGGED_FRACTION):
        result["untagged_skipped_reason"] = (f"{len(ids)}/{len(normalized_rows)} rows have no program -- exceeds the "
                                             f"{MAX_UNTAGGED_FRACTION:.0%} cap, likely a parser problem, nothing changed")
        print(f"Review-untagged skipped: {result['untagged_skipped_reason']}")
        return
    locs: List[dict] = []
    for i in range(0, len(ids), PATCH_BATCH):
        locs += await _get_all(session, (
            f"{base}/rest/v1/locations?select=location_id,location_name,listing_status,"
            f"organization:organizations(organization_name)"
            f"&achc_source_id=in.({','.join(ids[i:i + PATCH_BATCH])})"), key)
    published = [l for l in locs if l.get("listing_status") == "published"]
    print(f"Review-untagged: {len(ids)} listed tonight with no program; {len(published)} currently published")
    if dry_run or not locs:
        return
    result["untagged_to_review"] = await _patch_locations(
        session, base, key, [l["location_id"] for l in published], {"listing_status": "needs_review"})
    already = await _open_review_location_ids(session, base, key, [l["location_id"] for l in locs],
                                              UNTAGGED_SUMMARY_PREFIX)
    now = datetime.now(timezone.utc).isoformat()
    items = [{"location_id": l["location_id"], "review_type": "other", "severity": "medium", "review_status": "open",
              "issue_summary": untagged_summary((l.get("organization") or {}).get("organization_name")
                                                or l.get("location_name")),
              "opened_at": now} for l in locs if l["location_id"] not in already]
    for i in range(0, len(items), PATCH_BATCH):
        async with session.post(f"{base}/rest/v1/review_queue_items", json=items[i:i + PATCH_BATCH],
                                headers={**_headers(key), "Prefer": "return=minimal"},
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status not in (200, 201, 204):
                raise Exception(f"review insert failed: HTTP {resp.status} {(await resp.text())[:200]}")
    result["reviews_opened"] = len(items)


async def retire_unseen_locations(normalized_rows: List[dict], run_metadata: dict, dry_run: bool = False) -> dict:
    """Run both rules. Returns a summary dict for the run log."""
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    result = {"dry_run": dry_run, "unpublished": 0, "untagged_to_review": 0, "reviews_opened": 0}
    if not base or not key:
        result["skipped_reason"] = "Supabase credentials not set"
        return result
    async with aiohttp.ClientSession() as session:
        await _unpublish_unlisted(session, base, key, normalized_rows, run_metadata, dry_run, result)
        await _review_untagged(session, base, key, normalized_rows, dry_run, result)
    if dry_run:
        print("Dry run: no changes written")
    print(f"Directory sync: unpublished {result['unpublished']} no-longer-listed; "
          f"moved {result['untagged_to_review']} care-type-unknown to review ({result['reviews_opened']} items opened)")
    return result
