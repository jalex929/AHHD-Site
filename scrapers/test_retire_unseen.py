"""Tests for the hide-listings-ACHC-no-longer-lists step (retire_unseen.py).

These cover the decision logic only (no network): when it is safe to act,
who counts as unseen, the mass-disappearance cap, and the duplicate-conflict
protection.

Run with:  pytest scrapers/test_retire_unseen.py -v
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from retire_unseen import (  # noqa: E402
    review_summary,
    run_is_safe_to_retire,
    scrape_reference_time,
    select_unseen,
    within_cap,
)

GOOD_RUN = {
    "test_mode": False,
    "limit_locations": 0,
    "no_state_filter": True,
    "scrape_status": "complete",
    "programs_with_zero_rows": [],
    "started_at_utc": "2026-11-11T13:39:24",
}
REF = datetime(2026, 11, 11, 13, 39, tzinfo=timezone.utc)


def loc(id_, last_seen, achc=None):
    return {"location_id": id_, "source_last_seen_at": last_seen, "achc_source_id": achc,
            "organization": {"organization_name": f"Org {id_}"}}


# --- when it is safe to act at all -------------------------------------------

def test_complete_national_run_is_safe():
    assert run_is_safe_to_retire(GOOD_RUN) == (True, "ok")


def test_unsafe_runs_change_nothing():
    for change, reason_fragment in [
        ({"test_mode": True}, "test mode"),
        ({"limit_locations": 100}, "row-limited"),
        ({"no_state_filter": False}, "single-state"),
        ({"scrape_status": "incomplete_scrape"}, "scrape_status"),
        ({"programs_with_zero_rows": ["Hospice"]}, "0 rows"),
    ]:
        ok, why = run_is_safe_to_retire({**GOOD_RUN, **change})
        assert not ok and reason_fragment in why, (change, why)


def test_one_zero_row_program_blocks_even_though_scrape_says_complete():
    # achc_scraper marks a run "complete" with up to 2 zero-row programs;
    # with only 3 programs scraped, one empty program would look like
    # thousands of providers disappearing.
    ok, _ = run_is_safe_to_retire({**GOOD_RUN, "programs_with_zero_rows": ["Home Care"]})
    assert not ok


# --- reference time ------------------------------------------------------------

def test_reference_is_earliest_row_stamp():
    rows = [{"last_seen": "2026-11-11T13:45:00"}, {"last_seen": "2026-11-11T13:39:59"}]
    assert scrape_reference_time(rows, GOOD_RUN) == datetime(2026, 11, 11, 13, 39, 59, tzinfo=timezone.utc)


def test_reference_falls_back_to_scrape_start():
    assert scrape_reference_time([{}], GOOD_RUN) == datetime(2026, 11, 11, 13, 39, 24, tzinfo=timezone.utc)


# --- who counts as unseen --------------------------------------------------------

def test_seen_this_run_is_kept():
    assert select_unseen([loc("a", "2026-11-11T13:40:00+00:00")], REF, grace_days=2) == []


def test_one_missed_run_is_within_grace():
    assert select_unseen([loc("a", (REF - timedelta(days=1)).isoformat())], REF, grace_days=2) == []


def test_missing_past_grace_is_unseen():
    stale = loc("a", "2026-05-10T00:00:00+00:00")
    assert select_unseen([stale], REF, grace_days=2) == [stale]


def test_never_stamped_is_unseen():
    never = loc("a", None)
    assert select_unseen([never], REF) == [never]


def test_naive_timestamps_are_read_as_utc():
    assert select_unseen([loc("a", "2026-11-10T13:40:00")], REF, grace_days=2) == []


# --- the mass-disappearance cap -------------------------------------------------

def test_cap_allows_normal_churn_and_the_one_time_cleanup():
    assert within_cap(0, 6517)
    assert within_cap(5, 6517)
    assert within_cap(822, 6517)  # the 2026-11-11 backlog, ~12.6%


def test_cap_blocks_mass_disappearance():
    assert not within_cap(2000, 6517)
    assert not within_cap(1, 0)


# --- listed by ACHC tonight = never hidden ------------------------------------------

def test_listed_tonight_is_never_hidden_even_with_an_old_date():
    # The merge skips ~230 rows a night on orphaned-duplicate conflicts,
    # freezing the real listing's last-seen date. ACHC still lists them.
    frozen = loc("a", "2026-05-10T00:00:00+00:00", achc="75467")
    assert select_unseen([frozen], REF, seen_ids=["75467"], grace_days=2) == []


def test_not_listed_tonight_and_old_is_hidden():
    gone = loc("a", "2026-05-10T00:00:00+00:00", achc="80572")
    assert select_unseen([gone], REF, seen_ids=["75467"], grace_days=2) == [gone]


def test_orphan_without_an_id_is_judged_by_date_alone():
    orphan = loc("a", "2026-05-10T00:00:00+00:00", achc=None)
    assert select_unseen([orphan], REF, seen_ids=["75467"], grace_days=2) == [orphan]


def test_ids_compare_as_strings():
    assert select_unseen([loc("a", None, achc=75467)], REF, seen_ids=["75467"]) == []


# --- review item wording ---------------------------------------------------------

def test_review_summary_says_why_and_when():
    text = review_summary(loc("a", "2026-05-10T00:00:00+00:00"))
    assert text.startswith("Not in ACHC accredited list (last seen 2026-05-10): Org a.")
    assert "Hidden until verified" in text
    assert "never recorded" in review_summary(loc("b", None))
