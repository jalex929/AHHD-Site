"""Tests for ACHC accreditation-detail parsing (accreditation_helper.py).

Fixtures are real GetAccreditedServicesListByCompanyID responses captured
2026-11-11. The old parser returned [] for every one of these, which left
1,523 currently accredited providers with no care type (hidden from search).

Run with:  pytest scrapers/test_accreditation_helper.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from accreditation_helper import canonical_program, merge_ajax_services, parse_accreditation_html  # noqa: E402

HOME_HEALTH_DEEMED = (
    '<p style="padding:0; margin:5px auto 0 0"><strong>Dates:</strong> 10/21/2025 through 10/21/2028</p>'
    '<p style="padding:0; margin:5px auto 0 10px"><strong>Program:</strong> Home Health (Deemed)</p>'
    '<p style="padding:0; margin:0 auto 0 10px"><strong>Services:</strong> Home Health Aide Services,  '
    'Medical Social Services,  Occupational Therapy Services,  Physical Therapy Services,  '
    'Skilled Nursing Services,  Speech Therapy Services</p>'
)
HOME_CARE = (
    '<p style="padding:0; margin:5px auto 0 0"><strong>Dates:</strong> 2/6/2026 through 2/5/2029</p>'
    '<p style="padding:0; margin:5px auto 0 10px"><strong>Program:</strong> Home Care</p>'
    '<p style="padding:0; margin:0 auto 0 10px"><strong>Services:</strong> Home Care Aide Services,  '
    'Home Care Nursing Services</p>'
)
HOSPICE_TWO_PERIODS = (
    '<p style="padding:0; margin:5px auto 0 0"><strong>Dates:</strong> 10/12/2023 through 10/12/2026</p>'
    '<p style="padding:0; margin:5px auto 0 10px"><strong>Program:</strong> Hospice (Deemed under parent provider number)</p>'
    '<p style="padding:0; margin:0 auto 0 10px"><strong>Services:</strong> Hospice Care Services</p>'
    '<p style="padding:0; margin:5px auto 0 0"><strong>Dates:</strong> 10/12/2026 through 10/12/2029</p>'
    '<p style="padding:0; margin:5px auto 0 10px"><strong>Program:</strong> Hospice (Deemed under parent provider number)</p>'
    '<p style="padding:0; margin:0 auto 0 10px"><strong>Services:</strong> Hospice Care Services</p>'
)


# --- the bug this fixes -----------------------------------------------------

def test_reads_program_value_after_the_bold_label():
    assert parse_accreditation_html(HOME_HEALTH_DEEMED) == ["Home Health"]


def test_home_care_program():
    assert parse_accreditation_html(HOME_CARE) == ["Home Care"]


def test_deemed_qualifier_and_repeated_periods_collapse_to_one_program():
    assert parse_accreditation_html(HOSPICE_TWO_PERIODS) == ["Hospice"]


def test_multiple_programs_are_all_returned_sorted():
    html = HOSPICE_TWO_PERIODS + HOME_HEALTH_DEEMED
    assert parse_accreditation_html(html) == ["Home Health", "Hospice"]


# --- things that must NOT count as a program --------------------------------

def test_services_line_is_not_evidence_of_a_program():
    # "Home Care Aide Services" appears under Home Health providers too; only
    # the Program: field decides the care type.
    html = '<p><strong>Services:</strong> Home Care Aide Services, Hospice Care Services</p>'
    assert parse_accreditation_html(html) == []


def test_labels_alone_are_not_programs():
    html = "<p><strong>Dates:</strong></p><p><strong>Program:</strong></p><p><strong>Services:</strong></p>"
    assert parse_accreditation_html(html) == []


def test_empty_or_missing_response():
    # What ACHC returns for a company that is no longer accredited.
    assert parse_accreditation_html("") == []
    assert parse_accreditation_html(None) == []


def test_unknown_program_is_ignored_not_guessed():
    html = "<p><strong>Program:</strong> Something Brand New</p>"
    assert parse_accreditation_html(html) == []


# --- program name mapping ---------------------------------------------------

def test_canonical_program_strips_qualifiers_and_case():
    assert canonical_program("Home Health (Deemed)") == "Home Health"
    assert canonical_program("  HOSPICE  ") == "Hospice"
    assert canonical_program("Hospice (Deemed under parent provider number)") == "Hospice"


def test_canonical_program_prefers_the_longest_match():
    # "PCAB Compounding Pharmacy" must not be read as plain "Pharmacy".
    assert canonical_program("PCAB Compounding Pharmacy") == "PCAB Compounding Pharmacy"
    assert canonical_program("Pharmacy") == "Pharmacy"


def test_label_variants_are_tolerated():
    assert parse_accreditation_html("<b>Program</b>: Home Care") == ["Home Care"]
    assert parse_accreditation_html("<STRONG>Program:</STRONG>Hospice") == ["Hospice"]


# --- merging into normalized rows -------------------------------------------

def test_ajax_programs_replace_text_guesses():
    # Live example 94824: search text suggested Home Health + Private Duty,
    # ACHC's own record says Home Care.
    rows = [{"achc_company_id": "94824", "services": ["Home Health", "Private Duty"]}]
    merge_ajax_services(rows, {"94824": ["Home Care"]})
    assert rows[0]["services"] == ["Home Care"]


def test_empty_ajax_result_leaves_row_untouched():
    rows = [{"achc_company_id": "1", "services": ["Hospice"]}]
    merge_ajax_services(rows, {"1": []})
    assert rows[0]["services"] == ["Hospice"]


def test_fills_previously_empty_care_type():
    rows = [{"achc_company_id": "96311", "services": []}]
    merge_ajax_services(rows, {"96311": parse_accreditation_html(HOME_HEALTH_DEEMED)})
    assert rows[0]["services"] == ["Home Health"]
