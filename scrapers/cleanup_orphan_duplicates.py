"""Find (and, only with --apply, remove) orphaned duplicate location rows.

Background (traced 2026-11-11):
  Imports before 2026-05-31 stored some provider names HTML-encoded
  ("Aging, Disability &amp; Transit Services"), so the same provider ended up
  as TWO organizations and TWO location rows at the same address:
    - the REAL row: has the ACHC accreditation ID, under the encoded org
    - an ORPHAN copy: no ACHC ID, under the clean-named org
  Each night the merge finds the real row by ID and tries to move it to the
  clean org, which collides with the orphan on the one-location-per-org-per-
  address unique index (locations_org_address_idx). The row is skipped, so
  the real listing never updates (~230 skips/night), and the orphan -- also
  published -- makes the provider appear twice on the site.
  The scraper has HTML-unescaped names since 2026-05-31 (874814a), so no new
  orphans are being created; this cleans up the old ones.

What counts as an orphan duplicate (all must hold):
  - achc_source_id IS NULL and source_system = 'achc' (never a founder-entered row)
  - another location at the exact same address (line 1, city, state, zip)
    HAS an ACHC ID
  - the two organization names match once HTML-decoded and reduced to
    letters/digits

Removing an orphan deletes its dependent rows first (service links,
accreditation records, review items), then the location. A full JSON backup
of everything removed is written before any delete. Provider pages are
addressed by ACHC ID (src/lib/slug.ts), so orphans have no page or sitemap
URL to break.

Usage:
  python cleanup_orphan_duplicates.py                 # dry run: report + CSV, no changes
  python cleanup_orphan_duplicates.py --apply BACKUP_DIR
Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (orphans may be unpublished, so
the anon key can't see them all).
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

CHILD_TABLES = ("location_service_types", "accreditation_records", "review_queue_items")
BATCH = 100


# ---------------------------------------------------------------------------
# Pure logic (unit tested in test_cleanup_orphan_duplicates.py)
# ---------------------------------------------------------------------------

def name_key(name: str) -> str:
    """Compare names the way a person would: decode HTML (twice, for &amp;amp;), letters/digits only."""
    return re.sub(r"[^a-z0-9]", "", html.unescape(html.unescape(name or "")).lower())


def address_key(loc: dict) -> Tuple[str, str, str, str]:
    return (
        (loc.get("address_line_1") or "").strip().lower(),
        (loc.get("city") or "").strip().lower(),
        (loc.get("state") or "").strip().upper(),
        (loc.get("postal_code") or "").strip()[:5],
    )


def org_name(loc: dict) -> str:
    return (loc.get("organization") or {}).get("organization_name") or ""


def find_orphan_pairs(locations: List[dict]) -> List[Tuple[dict, dict]]:
    """Return (orphan, real) pairs. Each orphan appears at most once."""
    by_addr: Dict[Tuple, List[dict]] = defaultdict(list)
    for loc in locations:
        by_addr[address_key(loc)].append(loc)
    pairs = []
    for group in by_addr.values():
        reals = [l for l in group if l.get("achc_source_id")]
        for cand in group:
            if cand.get("achc_source_id") or cand.get("source_system") != "achc":
                continue
            match = next((r for r in reals if name_key(org_name(r)) == name_key(org_name(cand))), None)
            if match:
                pairs.append((cand, match))
    return pairs


# ---------------------------------------------------------------------------
# Supabase I/O
# ---------------------------------------------------------------------------

def _req(method: str, path: str, body=None, extra_headers=None):
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", **(extra_headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}/rest/v1/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def fetch_all(path: str) -> List[dict]:
    rows, start = [], 0
    while True:
        page = _req("GET", path, extra_headers={"Range": f"{start}-{start + 999}"})
        rows.extend(page)
        if len(page) < 1000:
            return rows
        start += 1000


def load_locations() -> List[dict]:
    sel = ("location_id,achc_source_id,source_system,listing_status,accepts_public_display,address_line_1,city,state,"
           "postal_code,source_last_seen_at,created_at,organization_id,organization:organizations(organization_name)")
    return fetch_all(f"locations?select={sel}&order=location_id")


def backup_and_delete(orphans: List[dict], backup_dir: str) -> dict:
    os.makedirs(backup_dir, exist_ok=True)
    ids = [o["location_id"] for o in orphans]
    backup = {"created_at": datetime.now(timezone.utc).isoformat(), "locations": [], **{t: [] for t in CHILD_TABLES}}
    for i in range(0, len(ids), BATCH):
        chunk = ",".join(ids[i:i + BATCH])
        backup["locations"] += _req("GET", f"locations?select=*&location_id=in.({chunk})")
        for t in CHILD_TABLES:
            backup[t] += _req("GET", f"{t}?select=*&location_id=in.({chunk})")
    path = os.path.join(backup_dir, f"orphan_duplicates_backup_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=1, default=str)
    if len(backup["locations"]) != len(ids):
        raise SystemExit(f"Backup has {len(backup['locations'])} of {len(ids)} rows -- aborting before any delete")
    print(f"Backup written: {path}")

    deleted = defaultdict(int)
    for i in range(0, len(ids), BATCH):
        chunk = ",".join(ids[i:i + BATCH])
        for t in CHILD_TABLES:
            gone = _req("DELETE", f"{t}?location_id=in.({chunk})", extra_headers={"Prefer": "return=representation"})
            deleted[t] += len(gone or [])
        gone = _req("DELETE", f"locations?location_id=in.({chunk})&achc_source_id=is.null",
                    extra_headers={"Prefer": "return=representation"})
        deleted["locations"] += len(gone or [])
    return {"backup": path, **deleted}


def main(argv: List[str]) -> int:
    apply = "--apply" in argv
    pairs = find_orphan_pairs(load_locations())
    published = sum(1 for o, _ in pairs if o.get("listing_status") == "published" and o.get("accepts_public_display"))
    print(f"Orphan duplicates found: {len(pairs)} ({published} currently published and shown on the site)")

    report = "orphan_duplicates_report.csv"
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["orphan_location_id", "orphan_org_name", "orphan_published", "orphan_last_seen",
                    "real_location_id", "real_achc_id", "real_org_name", "address", "city", "state", "zip"])
        for o, r in pairs:
            w.writerow([o["location_id"], org_name(o), o.get("listing_status") == "published" and o.get("accepts_public_display"),
                        str(o.get("source_last_seen_at"))[:10], r["location_id"], r["achc_source_id"], org_name(r),
                        o.get("address_line_1"), o.get("city"), o.get("state"), o.get("postal_code")])
    print(f"Report: {os.path.abspath(report)}")

    if not apply:
        print("Dry run: nothing changed. Re-run with --apply BACKUP_DIR to back up and remove these rows.")
        return 0
    backup_dir = argv[argv.index("--apply") + 1] if len(argv) > argv.index("--apply") + 1 else "backups"
    print(backup_and_delete([o for o, _ in pairs], backup_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
