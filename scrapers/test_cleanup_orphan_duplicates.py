"""Tests for orphan-duplicate detection (cleanup_orphan_duplicates.py).

The real case from 2026-11-11: 105 Lawsonville Avenue, Reidsville NC.
Real row (ACHC 31248) under "Aging, Disability &amp; Transit Services";
orphan copy (no ID) under "Aging, Disability & Transit Services".

Run with:  pytest scrapers/test_cleanup_orphan_duplicates.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from cleanup_orphan_duplicates import find_orphan_pairs, name_key  # noqa: E402


def loc(id_, achc, org, street="105 Lawsonville Avenue", city="Reidsville", state="NC", zip_="27320", source="achc"):
    return {"location_id": id_, "achc_source_id": achc, "source_system": source, "address_line_1": street,
            "city": city, "state": state, "postal_code": zip_, "organization": {"organization_name": org}}


REAL = loc("real", "31248", "Aging, Disability &amp; Transit Services")
ORPHAN = loc("orphan", None, "Aging, Disability & Transit Services")


def test_finds_the_reidsville_pair():
    assert find_orphan_pairs([REAL, ORPHAN]) == [(ORPHAN, REAL)]


def test_name_key_decodes_html_and_ignores_punctuation():
    assert name_key("Aging, Disability &amp; Transit Services") == name_key("Aging Disability & Transit Services")
    assert name_key("A &amp;amp; B") == name_key("A & B")


def test_different_provider_at_the_same_address_is_not_a_duplicate():
    neighbor = loc("n", None, "Some Other Home Care LLC")
    assert find_orphan_pairs([REAL, neighbor]) == []


def test_same_name_at_a_different_address_is_not_a_duplicate():
    elsewhere = loc("e", None, "Aging, Disability & Transit Services", street="9 Elm St")
    assert find_orphan_pairs([REAL, elsewhere]) == []


def test_founder_entered_rows_are_never_touched():
    manual = loc("m", None, "Aging, Disability & Transit Services", source="manual")
    assert find_orphan_pairs([REAL, manual]) == []


def test_two_real_accreditations_at_one_address_are_not_orphans():
    # e.g. Home Health and Hospice accredited separately at one address.
    sibling = loc("s", "31249", "Aging, Disability & Transit Services")
    assert find_orphan_pairs([REAL, sibling]) == []


def test_orphan_with_no_real_row_is_left_alone():
    # Not a duplicate of anything -- the hide-stale step handles these.
    assert find_orphan_pairs([ORPHAN]) == []


def test_zip_plus_four_and_case_still_match():
    real = loc("r", "1", "Acme Hospice", street="1 Main St", zip_="60601-1234")
    orphan = loc("o", None, "ACME HOSPICE", street="1 MAIN ST", zip_="60601")
    assert find_orphan_pairs([real, orphan]) == [(orphan, real)]
