"""Tests for the live-content health checks (check_content_health.py).

Each check is exercised against the two real problems found 2026-11-11 to
prove it would have caught them, and against healthy data to prove it stays
quiet (a check that cries wolf gets ignored).

Run with:  pytest scrapers/test_content_health.py -v
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from check_content_health import (  # noqa: E402
    check_care_type_coverage,
    check_frozen_listings,
    check_listed_but_hidden,
    check_stale_published,
)

REF = datetime(2026, 11, 11, 13, 39, tzinfo=timezone.utc)


def rows(with_type, without_type):
    return ([{"achc_company_id": str(i), "services": ["Home Health"]} for i in range(with_type)]
            + [{"achc_company_id": f"x{i}", "services": []} for i in range(without_type)])


# --- care type coverage ------------------------------------------------------

def test_coverage_fails_on_the_2026_11_11_bug():
    # 1,529 of 7,370 scraped providers had no program.
    result = check_care_type_coverage(rows(7370 - 1529, 1529))
    assert not result["ok"] and "79.3%" in result["detail"]


def test_coverage_passes_when_nearly_all_have_a_program():
    assert check_care_type_coverage(rows(7360, 10))["ok"]


def test_coverage_fails_on_an_empty_scrape():
    assert not check_care_type_coverage([])["ok"]


def test_coverage_accepts_comma_separated_strings():
    assert check_care_type_coverage([{"services": "Home Care, Hospice"}], minimum=1.0)["ok"]


# --- stale published listings -------------------------------------------------

def pub(last_seen, achc="1"):
    return {"achc_source_id": achc, "source_last_seen_at": last_seen}


def test_stale_fails_on_listings_unseen_since_may():
    published = [pub("2026-11-11T13:40:00")] * 10 + [pub("2026-05-10T00:00:00")] * 3
    result = check_stale_published(published, REF, grace_days=2)
    assert not result["ok"] and result["count"] == 3


def test_stale_counts_never_stamped_listings():
    assert check_stale_published([pub(None)], REF, grace_days=2)["count"] == 1


def test_stale_passes_within_grace():
    published = [pub("2026-11-11T13:40:00"), pub("2026-11-10T13:40:00")]
    assert check_stale_published(published, REF, grace_days=2)["ok"]


# --- listed by ACHC but hidden on the site -------------------------------------

def test_hidden_fails_when_many_listed_providers_are_missing():
    scraped = rows(100, 0)
    visible = [str(i) for i in range(80)]
    result = check_listed_but_hidden(scraped, visible)
    assert not result["ok"] and result["examples"][0] == "80"


def test_hidden_tolerates_a_few_conflicts_or_pending_geocodes():
    scraped = rows(100, 0)
    visible = [str(i) for i in range(98)]
    assert check_listed_but_hidden(scraped, visible)["ok"]


def test_hidden_ignores_providers_outside_the_shown_care_types():
    scraped = [{"achc_company_id": "1", "services": ["Pharmacy"]}, {"achc_company_id": "2", "services": ["Hospice"]}]
    assert check_listed_but_hidden(scraped, ["2"])["ok"]


# --- stale vs frozen are told apart ---------------------------------------------

def test_listing_in_tonights_data_is_frozen_not_stale():
    published = [pub("2026-05-10T00:00:00", achc="75467")]
    assert check_stale_published(published, REF, seen_ids=["75467"], grace_days=2)["count"] == 0
    frozen = check_frozen_listings(published, REF, ["75467"], grace_days=2)
    assert frozen["ok"] and frozen["warn"] and frozen["count"] == 1


def test_frozen_is_quiet_when_rows_update():
    published = [pub("2026-11-11T13:40:00", achc="1")]
    assert not check_frozen_listings(published, REF, ["1"], grace_days=2)["warn"]
