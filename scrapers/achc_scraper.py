import asyncio
import html as _html
import json
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Set, Tuple

import aiohttp
from dotenv import load_dotenv
from geocode_helper import geocode_locations, geocode_locations_nominatim_fallback, print_dedup_report
from places_helper import enrich_websites
from accreditation_helper import fetch_accreditation_services, merge_ajax_services
from retire_unseen import retire_unseen_locations
from playwright.async_api import async_playwright

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

load_dotenv()
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

AMS_URL = "https://ams.achc.org/accredited_organizations.aspx"

DEFAULT_PROGRAMS = [
    "Home Care",
    "Home Health",
    "Hospice",
    "Ambulatory Care",
    "Assisted Living",
    "Behavioral Health",
    "Dentistry",
    "Home Infusion Therapy",
    "Palliative Care",
    "Renal Dialysis",
    "Sleep",
    "Community Retail",
    "DMEPOS",
    "Pharmacy",
    "Private Duty",
    "Healthcare Staffing Services Certification",
    "PCAB Compounding Pharmacy",
    "Long-Term Care Dialysis Certification",
    "Telehealth Certification",
    "ACHC Inspection Services",
]

DEFAULT_TRIGGER_STATE = "Texas"

# 0 means unlimited / full pull
LIMIT_LOCATIONS = int(os.getenv("LIMIT_LOCATIONS", "0"))

GOOGLE_SHEETS_URL = os.getenv("GOOGLE_SHEETS_WEB_APP_URL")
SCRAPER_SECRET = os.getenv("SCRAPER_SECRET", "")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
TEST_PROGRAMS_ENV = os.getenv("TEST_PROGRAMS", "").strip()
TRIGGER_STATE = os.getenv("TRIGGER_STATE", DEFAULT_TRIGGER_STATE).strip()

# True = do not select a state at all
NO_STATE_FILTER = os.getenv("NO_STATE_FILTER", "true").lower() == "true"

ENABLE_AJAX_ACCREDITATION = os.getenv("ENABLE_AJAX_ACCREDITATION", "true").lower() == "true"
ENABLE_WEBSITE_ENRICHMENT = os.getenv("ENABLE_WEBSITE_ENRICHMENT", "true").lower() == "true"
ENABLE_GEOCODING        = os.getenv("ENABLE_GEOCODING", "true").lower() == "true"
ENABLE_DIRECT_SUPABASE   = os.getenv("ENABLE_DIRECT_SUPABASE", "true").lower() == "true"
ENABLE_RETIRE_UNSEEN     = os.getenv("ENABLE_RETIRE_UNSEEN", "true").lower() == "true"
RETIRE_UNSEEN_DRY_RUN    = os.getenv("RETIRE_UNSEEN_DRY_RUN", "false").lower() == "true"

SUPABASE_URL             = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

ENABLE_COVERAGE_DIAGNOSTIC = os.getenv("ENABLE_COVERAGE_DIAGNOSTIC", "true").lower() == "true"
COVERAGE_JSON_PATH = os.getenv("COVERAGE_JSON_PATH", "coverage_summary.json").strip()

ENABLE_ZERO_RESULT_DEBUG = os.getenv("ENABLE_ZERO_RESULT_DEBUG", "true").lower() == "true"
ZERO_RESULT_DEBUG_DIR = os.getenv("ZERO_RESULT_DEBUG_DIR", "debug_artifacts").strip()
ZERO_RESULT_DEBUG_TARGETS_ENV = os.getenv("ZERO_RESULT_DEBUG_TARGETS", "Home Health,Community Retail").strip()
ZERO_RESULT_DEBUG_TARGETS = [p.strip() for p in ZERO_RESULT_DEBUG_TARGETS_ENV.split(",") if p.strip()]

PROGRAM_SELECTION_WAIT_MS = int(os.getenv("PROGRAM_SELECTION_WAIT_MS", "2500"))
STATE_SELECTION_WAIT_MS = int(os.getenv("STATE_SELECTION_WAIT_MS", "1500"))
PRE_SEARCH_WAIT_MS = int(os.getenv("PRE_SEARCH_WAIT_MS", "2500"))
POST_SEARCH_WAIT_MS = int(os.getenv("POST_SEARCH_WAIT_MS", "5000"))

PROGRAMS = (
    [p.strip() for p in TEST_PROGRAMS_ENV.split(",") if p.strip()]
    if TEST_PROGRAMS_ENV
    else DEFAULT_PROGRAMS
)

if not GOOGLE_SHEETS_URL:
    if not ENABLE_DIRECT_SUPABASE:
        print("WARNING: no write destination active (GOOGLE_SHEETS_WEB_APP_URL unset, ENABLE_DIRECT_SUPABASE=false) — this run will scrape and report only, nothing will be written anywhere")
    else:
        print("WARNING: GOOGLE_SHEETS_WEB_APP_URL is not set — Google Sheets writes will be skipped (ENABLE_DIRECT_SUPABASE is active)")

CHECKPOINT_FILE = os.getenv("CHECKPOINT_FILE", "scrape_checkpoint.json")
# Checkpoints older than this are ignored and a fresh scrape is run instead
CHECKPOINT_MAX_AGE_HOURS = float(os.getenv("CHECKPOINT_MAX_AGE_HOURS", "24"))

STATE_ABBR_MAP = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}

COUNTRY_PREFERRED_LABELS = [
    "USA",
    "United States",
    "United States of America",
    "U.S.A.",
    "U.S.",
    "US",
]

EXPECTED_PROGRAMS: Set[str] = set(DEFAULT_PROGRAMS)

CANONICAL_PROGRAM_MAP: Dict[str, str] = {
    "home care": "Home Care",
    "home health": "Home Health",
    "hospice": "Hospice",
    "ambulatory care": "Ambulatory Care",
    "assisted living": "Assisted Living",
    "behavioral health": "Behavioral Health",
    "dentistry": "Dentistry",
    "home infusion therapy": "Home Infusion Therapy",
    "palliative care": "Palliative Care",
    "renal dialysis": "Renal Dialysis",
    "sleep": "Sleep",
    "community retail": "Community Retail",
    "dmepos": "DMEPOS",
    "pharmacy": "Pharmacy",
    "private duty": "Private Duty",
    "healthcare staffing services certification": "Healthcare Staffing Services Certification",
    "healthcare staffing": "Healthcare Staffing Services Certification",
    "pcab compounding pharmacy": "PCAB Compounding Pharmacy",
    "pcab compounding": "PCAB Compounding Pharmacy",
    "long-term care dialysis certification": "Long-Term Care Dialysis Certification",
    "long-term care dialysis": "Long-Term Care Dialysis Certification",
    "telehealth certification": "Telehealth Certification",
    "telehealth": "Telehealth Certification",
    "achc inspection services": "ACHC Inspection Services",
    "inspection services": "ACHC Inspection Services",
}


async def polite_pause(min_seconds: float = 0.12, max_seconds: float = 0.28):
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def get_result_scope(trigger_state: str, no_state_filter: bool) -> str:
    if no_state_filter:
        return "National raw ACHC pull; no state selected"
    return f"State-filtered ACHC pull; state = {trigger_state}"


def split_city_state_zip(text: str) -> Tuple[str, str, str]:
    if not text:
        return "", "", ""

    match = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", text.strip())
    if not match:
        return "", "", ""

    return match.group(1), match.group(2), match.group(3)


# ---------------------------------------------------------------------------
# parse_raw_block — kept as a fallback; prefer parse_listing_html() instead.
# ---------------------------------------------------------------------------
def parse_raw_block(raw_text: str) -> Tuple[str, str, str]:
    """Fallback parser: splits raw inner_text() by newlines and uses regex.

    Returns (raw_name_line, raw_address_block, parsed_state_abbr).
    Use parse_listing_html() when the first <td> innerHTML is available.
    """
    if not raw_text:
        return "", "", ""

    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    cleaned_lines = []

    for line in lines:
        cleaned = line.replace("Show/Hide Accreditation Details", "").strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t")
        if cleaned:
            cleaned_lines.append(cleaned)

    if not cleaned_lines:
        return "", "", ""

    raw_name_line = cleaned_lines[0]
    city_state_zip = ""
    address_lines = []

    for line in cleaned_lines[1:]:
        if re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$", line):
            city_state_zip = line
            break
        address_lines.append(line)

    raw_address_block = " | ".join(address_lines)
    _, parsed_state_abbr, _ = split_city_state_zip(city_state_zip)
    return raw_name_line, raw_address_block, parsed_state_abbr


# ---------------------------------------------------------------------------
# parse_listing_html — preferred HTML-aware parser (T-010, T-012)
# ---------------------------------------------------------------------------
_CITY_STATE_ZIP_RE = re.compile(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$")
_DIV_TITLE_INFO_RE = re.compile(r"^divTitleInfo(\d+)$")
_DBA_PREFIX_RE = re.compile(r"^d/b/a\s+", re.IGNORECASE)


def _parse_listing_html_bs4(html: str) -> dict:
    """Parse first-<td> innerHTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")

    # --- legal name ---
    strong_tag = soup.find("strong")
    legal_name = strong_tag.get_text(strip=True) if strong_tag else ""

    # --- company ID from <div id="divTitleInfo{id}"> ---
    achc_company_id = ""
    div_tag = soup.find("div", id=_DIV_TITLE_INFO_RE)
    if div_tag:
        m = _DIV_TITLE_INFO_RE.match(div_tag.get("id", ""))
        if m:
            achc_company_id = m.group(1)

    # --- walk children of the top-level element to collect text segments ---
    # BeautifulSoup parses the fragment; its children are the top-level nodes.
    # We treat the whole soup as the container and walk its direct children,
    # collecting NavigableString text between <br> tags, stopping at the <div>.
    text_segments = []
    past_strong = False

    # Use .children on the soup object (the parsed fragment's root).
    # If bs4 wraps the content in a <html><body> tree we navigate down.
    container = soup
    # Try to find the immediate parent wrapping all content.
    # For a fragment, soup itself acts as a document; iterate its descendants
    # at the correct depth by walking the first meaningful parent.
    body = soup.find("body")
    if body:
        container = body

    for node in container.children:
        node_name = getattr(node, "name", None)

        # Stop collecting when we hit the divTitleInfo div
        if node_name == "div":
            div_id = node.get("id", "") if hasattr(node, "get") else ""
            if _DIV_TITLE_INFO_RE.match(div_id):
                break

        # Skip <br> tags — they act as separators; text after them is already
        # in a following NavigableString node
        if node_name == "br":
            continue

        # The <strong> tag holds the legal name (already captured above)
        if node_name == "strong":
            past_strong = True
            continue

        # Collect NavigableString nodes that appear after the <strong>
        if past_strong and node_name is None:
            segment = str(node).strip()
            if segment:
                text_segments.append(segment)

    # --- classify segments ---
    dba_name = ""
    address_lines = []
    city = ""
    state = ""
    zip_code = ""

    for seg in text_segments:
        # Check city/state/zip first (most specific)
        csz_match = _CITY_STATE_ZIP_RE.match(seg)
        if csz_match:
            city = csz_match.group(1).strip()
            state = csz_match.group(2).strip()
            zip_code = csz_match.group(3).strip()
            continue

        # DBA: first segment starting with d/b/a (case-insensitive)
        if not dba_name and _DBA_PREFIX_RE.match(seg):
            dba_name = _DBA_PREFIX_RE.sub("", seg).strip()
            continue

        # Everything else is a street address line
        address_lines.append(seg)

    street_address = ", ".join(address_lines)

    return {
        "legal_name": legal_name,
        "dba_name": dba_name,
        "street_address": street_address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "achc_company_id": achc_company_id,
    }


def _parse_listing_html_regex(html: str) -> dict:
    """Fallback parser for when BeautifulSoup is not installed.

    Extracts fields from the first-<td> innerHTML using regex only.
    """
    # --- legal name ---
    strong_match = re.search(
        r'<strong[^>]*>\s*(.*?)\s*</strong>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    legal_name = strong_match.group(1).strip() if strong_match else ""

    # --- company ID ---
    achc_company_id = ""
    div_id_match = re.search(r'id="divTitleInfo(\d+)"', html, re.IGNORECASE)
    if div_id_match:
        achc_company_id = div_id_match.group(1)

    # --- extract text content between </strong> and the divTitleInfo <div> ---
    # Grab the slice of HTML between </strong> and the divTitleInfo div opening.
    body_slice = html
    strong_end = re.search(r'</strong>', html, re.IGNORECASE)
    if strong_end:
        body_slice = html[strong_end.end():]

    div_start = re.search(r'<div[^>]+id="divTitleInfo\d+"', body_slice, re.IGNORECASE)
    if div_start:
        body_slice = body_slice[:div_start.start()]

    # Strip all remaining tags and split on <br> boundaries.
    segments_raw = re.split(r'<br\s*/?>', body_slice, flags=re.IGNORECASE)
    segments = []
    for seg in segments_raw:
        clean = re.sub(r'<[^>]+>', '', seg).strip()
        clean = re.sub(r'\s+', ' ', clean).strip()
        if clean:
            segments.append(clean)

    # --- classify segments ---
    dba_name = ""
    address_lines = []
    city = ""
    state = ""
    zip_code = ""

    for seg in segments:
        csz_match = _CITY_STATE_ZIP_RE.match(seg)
        if csz_match:
            city = csz_match.group(1).strip()
            state = csz_match.group(2).strip()
            zip_code = csz_match.group(3).strip()
            continue

        if not dba_name and _DBA_PREFIX_RE.match(seg):
            dba_name = _DBA_PREFIX_RE.sub("", seg).strip()
            continue

        address_lines.append(seg)

    street_address = ", ".join(address_lines)

    return {
        "legal_name": legal_name,
        "dba_name": dba_name,
        "street_address": street_address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "achc_company_id": achc_company_id,
    }


def parse_listing_html(html: str) -> dict:
    """Parse the innerHTML of the first <td> in a provider listing row.

    Returns a dict with keys:
        legal_name, dba_name, street_address, city, state, zip, achc_company_id

    Uses BeautifulSoup when available, falls back to regex otherwise.
    """
    if not html:
        return {
            "legal_name": "",
            "dba_name": "",
            "street_address": "",
            "city": "",
            "state": "",
            "zip": "",
            "achc_company_id": "",
        }

    if _BS4_AVAILABLE:
        return _parse_listing_html_bs4(html)
    return _parse_listing_html_regex(html)


def detect_program_mentions(raw_text: str) -> List[str]:
    text_norm = normalize_text(raw_text)
    found = []

    for key, canonical in CANONICAL_PROGRAM_MAP.items():
        if key in text_norm:
            found.append(canonical)

    return sorted(set(found))


def summarize_unmapped_mentions(rows: List[dict]) -> List[str]:
    candidates = set()

    patterns = [
        r"Accreditation(?:\s+Commission)?[:\s]+([A-Za-z0-9&,\-/ ]{3,80})",
        r"Program(?:\s+Type)?[:\s]+([A-Za-z0-9&,\-/ ]{3,80})",
    ]

    for row in rows:
        raw_text = row.get("raw_text", "") or ""
        for pattern in patterns:
            for match in re.findall(pattern, raw_text, flags=re.IGNORECASE):
                candidate = re.sub(r"\s+", " ", match).strip(" .,:;|-")
                if not candidate:
                    continue

                candidate_norm = normalize_text(candidate)
                if candidate_norm not in CANONICAL_PROGRAM_MAP:
                    candidates.add(candidate)

    return sorted(candidates)


def normalize_row(row: dict) -> dict:
    raw_text = row.get("raw_text", "") or ""
    raw_name_line = row.get("raw_name_line", "") or ""
    raw_address_block = row.get("raw_address_block", "") or ""
    parsed_state_abbr = row.get("parsed_state_abbr", "") or ""
    achc_company_id = row.get("achc_company_id", "") or ""
    dba_name = row.get("dba_name", "") or ""

    # Split raw_address_block on " | " — parse_raw_block builds it as
    # " | ".join(address_lines) where address_lines are all lines before
    # the "City, ST ZIP" line.  Take the first non-empty segment as the
    # canonical street address (e.g. "123 Main St") and discard any
    # secondary lines (Suite, Attn, etc.) that may follow.
    if " | " in raw_address_block:
        parts = [p.strip() for p in raw_address_block.split(" | ") if p.strip()]
        street_address = parts[0] if parts else ""
    else:
        street_address = raw_address_block.strip()

    # Prefer HTML-parsed city/zip from the row (new fields).
    # Fall back to regex extraction from raw_text for backwards compatibility.
    city = row.get("city", "") or ""
    zip_code = row.get("zip", "") or ""

    if not city or not zip_code:
        city_zip_match = re.search(
            r'(.+),\s*[A-Z]{2}\s+(\d{5}(?:-\d{4})?)',
            raw_text,
            re.MULTILINE,
        )
        if city_zip_match:
            city_candidate = city_zip_match.group(1).strip()
            zip_candidate = city_zip_match.group(2).strip()
            # Validate: shouldn't be a full address line (no digits at start)
            if not re.match(r'^\d', city_candidate):
                if not city:
                    city = city_candidate
                if not zip_code:
                    zip_code = zip_candidate

    # Extract phone from raw_text
    phone_match = re.search(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', raw_text)
    phone = phone_match.group(0).strip() if phone_match else ""

    # Services as list — use detected_program_mentions (authoritative) not
    # searched_program_type (which is just the search trigger).
    detected = row.get("detected_program_mentions", "") or ""
    services = [s.strip() for s in detected.split(",") if s.strip()]

    provider_name = _html.unescape(raw_name_line.strip())
    street_address = _html.unescape(street_address)
    city = _html.unescape(city)
    dba_name = _html.unescape(dba_name)

    # Build provider_key: include achc_company_id when available for stability.
    if achc_company_id and parsed_state_abbr:
        provider_key = f"{safe_slug(provider_name)}_{parsed_state_abbr}_{achc_company_id}"
    elif parsed_state_abbr:
        provider_key = safe_slug(provider_name) + "_" + parsed_state_abbr
    else:
        provider_key = safe_slug(provider_name)

    return {
        "provider_key": provider_key,
        "provider_name": provider_name,
        "dba_name": dba_name,
        "street_address": street_address,
        "city": city,
        "state": parsed_state_abbr,
        "zip": zip_code,
        "phone": phone,
        "website": "",
        "services": services,
        "accrediting_body": "ACHC",
        "source_url": row.get("source_url", ""),
        "last_seen": row.get("last_seen", ""),
        "confidence_status": "verified",
        "searched_program_type": row.get("searched_program_type", ""),
        "result_scope": row.get("result_scope", ""),
        "achc_company_id": achc_company_id,
    }


def normalize_rows(raw_rows: List[dict]) -> List[dict]:
    """Normalize all raw rows and deduplicate by provider_key.

    When the same physical location appears in multiple program search results
    its ``detected_program_mentions`` may differ between occurrences.  We keep
    the first occurrence's non-service fields and merge the services lists from
    all occurrences into a single sorted, deduplicated list.
    """
    seen: Dict[str, int] = {}   # provider_key -> index in result list
    result: List[dict] = []

    for raw_row in raw_rows:
        normalized = normalize_row(raw_row)
        key = normalized["provider_key"]

        if key not in seen:
            seen[key] = len(result)
            result.append(normalized)
        else:
            # Merge services into the existing entry
            existing = result[seen[key]]
            merged_services = sorted(set(existing["services"]) | set(normalized["services"]))
            existing["services"] = merged_services

    return result


async def find_select_with_programs(page):
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        opts = [t.strip() for t in await sel.locator("option").all_inner_texts()]
        if "Home Care" in opts and "Home Health" in opts and "Hospice" in opts:
            return sel
    return None


async def find_select_with_states(page):
    state_markers = {"Illinois", "California", "Texas", "Florida", "New York", "District of Columbia"}
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        opts = [t.strip() for t in await sel.locator("option").all_inner_texts()]
        hits = sum(1 for o in opts if o in state_markers)
        if hits >= 4:
            return sel
    return None


async def find_country_select_and_value(page):
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        option_locator = sel.locator("option")
        option_count = await option_locator.count()

        options = []
        for j in range(option_count):
            opt = option_locator.nth(j)
            label = (await opt.inner_text()).strip()
            value = (await opt.get_attribute("value")) or ""
            options.append({"label": label, "value": value})

        normalized_labels = [normalize_text(o["label"]) for o in options]
        joined = " ".join(normalized_labels)
        if "usa" not in joined and "united states" not in joined:
            continue

        for preferred in COUNTRY_PREFERRED_LABELS:
            pref_norm = normalize_text(preferred)
            for option in options:
                if normalize_text(option["label"]) == pref_norm:
                    return sel, option["value"], option["label"]

        for option in options:
            label_norm = normalize_text(option["label"])
            if "united states" in label_norm or label_norm in {"usa", "us"}:
                return sel, option["value"], option["label"]

    return None, None, None


async def get_selected_option_info(select_locator) -> Dict[str, str]:
    value = (await select_locator.input_value()) or ""
    selected_option = select_locator.locator("option:checked")
    label = ""
    if await selected_option.count():
        label = (await selected_option.first.inner_text()).strip()

    return {
        "value": value,
        "label": label,
    }


async def click_search(page):
    selectors = [
        "input[value='Find']",
        "input[value='Search']",
        "button:has-text('Find')",
        "button:has-text('Search')",
    ]
    for sel in selectors:
        if await page.locator(sel).count():
            await page.locator(sel).first.click()
            return

    await page.keyboard.press("Enter")


async def wait_for_results(page):
    await page.wait_for_timeout(POST_SEARCH_WAIT_MS)


async def write_zero_result_debug_artifacts(
    page,
    program: str,
    trigger_state: str,
    no_state_filter: bool,
    selected_program_before_search: Dict[str, str],
    selected_state_before_search: Dict[str, str],
    detail_links_found: int,
):
    if not ENABLE_ZERO_RESULT_DEBUG:
        return

    os.makedirs(ZERO_RESULT_DEBUG_DIR, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    program_slug = safe_slug(program)
    state_slug = "no-state" if no_state_filter else safe_slug(trigger_state)

    screenshot_path = os.path.join(
        ZERO_RESULT_DEBUG_DIR,
        f"{timestamp}_{program_slug}_{state_slug}_zero_results.png",
    )
    html_path = os.path.join(
        ZERO_RESULT_DEBUG_DIR,
        f"{timestamp}_{program_slug}_{state_slug}_zero_results.html",
    )
    meta_path = os.path.join(
        ZERO_RESULT_DEBUG_DIR,
        f"{timestamp}_{program_slug}_{state_slug}_zero_results.json",
    )

    await page.screenshot(path=screenshot_path, full_page=True)

    html = await page.content()
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    meta = {
        "generated_at_utc": datetime.utcnow().isoformat(),
        "program_requested": program,
        "no_state_filter": no_state_filter,
        "trigger_state_requested": "" if no_state_filter else trigger_state,
        "result_scope": get_result_scope(trigger_state, no_state_filter),
        "selected_program_before_search": selected_program_before_search,
        "selected_state_before_search": selected_state_before_search,
        "detail_links_found": detail_links_found,
        "page_title": await page.title(),
        "page_url": page.url,
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f" Zero-result screenshot written to: {screenshot_path}")
    print(f" Zero-result HTML written to: {html_path}")
    print(f" Zero-result metadata written to: {meta_path}")


async def scrape_raw_rows(page, searched_program: str, trigger_state: str, no_state_filter: bool) -> Tuple[List[dict], int]:
    rows = []
    detail_links = page.locator("a:has-text('Show/Hide Accreditation Details')")
    link_count = await detail_links.count()
    print(f" Detail links found in DOM for {searched_program}: {link_count}")

    trigger_state_abbr = STATE_ABBR_MAP.get(trigger_state, "")

    for i in range(link_count):
        link = detail_links.nth(i)

        candidate_locators = [
            link.locator("xpath=ancestor::tr[1]"),
            link.locator("xpath=ancestor::div[1]"),
        ]

        raw_text = ""
        container_type = ""
        tr_locator = None

        for idx, candidate in enumerate(candidate_locators):
            try:
                candidate_text = await candidate.inner_text()
                if candidate_text and "Show/Hide Accreditation Details" in candidate_text:
                    raw_text = candidate_text.strip()
                    container_type = "tr" if idx == 0 else "div"
                    if idx == 0:
                        tr_locator = candidate
                    break
            except Exception:
                continue

        if not raw_text:
            continue

        cleaned_raw_text = raw_text.replace("Show/Hide Accreditation Details", "").strip()

        # --- Preferred path: parse innerHTML of the first <td> (T-012) ---
        parsed = {}
        if tr_locator is not None:
            try:
                td_html = await tr_locator.locator("td").first.inner_html()
                parsed = parse_listing_html(td_html)
            except Exception:
                parsed = {}

        if parsed.get("legal_name"):
            raw_name_line = parsed["legal_name"]
            raw_address_block = parsed["street_address"]
            parsed_state_abbr = parsed["state"]
            dba_name = parsed["dba_name"]
            city = parsed["city"]
            zip_code = parsed["zip"]
            achc_company_id = parsed["achc_company_id"]
        else:
            # Fallback to the legacy text-based parser
            raw_name_line, raw_address_block, parsed_state_abbr = parse_raw_block(cleaned_raw_text)
            dba_name = ""
            city = ""
            zip_code = ""
            achc_company_id = ""

        matches_trigger_state = False
        if not no_state_filter and parsed_state_abbr and trigger_state_abbr:
            matches_trigger_state = parsed_state_abbr == trigger_state_abbr

        detected_program_mentions = detect_program_mentions(cleaned_raw_text)

        rows.append(
            {
                "raw_index": i + 1,
                "container_type": container_type,
                "searched_program_type": searched_program,
                "search_trigger_state": "" if no_state_filter else trigger_state,
                "result_scope": get_result_scope(trigger_state, no_state_filter),
                "raw_name_line": raw_name_line,
                "raw_address_block": raw_address_block,
                "parsed_state_abbr": parsed_state_abbr,
                "matches_trigger_state": matches_trigger_state,
                "detected_program_mentions": ", ".join(detected_program_mentions),
                "raw_text": cleaned_raw_text,
                "source_url": AMS_URL,
                "last_seen": datetime.utcnow().isoformat(),
                # New fields from HTML-aware parser
                "achc_company_id": achc_company_id,
                "dba_name": dba_name,
                "city": city,
                "zip": zip_code,
            }
        )

        if LIMIT_LOCATIONS > 0 and len(rows) >= LIMIT_LOCATIONS:
            break

    return rows, link_count


async def select_dropdown_with_verification(select_locator, label: str, wait_ms: int, select_name: str) -> Dict[str, Dict[str, str]]:
    before = await get_selected_option_info(select_locator)
    await select_locator.select_option(label=label)
    await asyncio.sleep(wait_ms / 1000)
    after = await get_selected_option_info(select_locator)

    print(
        f" {select_name} selection requested='{label}' | "
        f"before_label='{before['label']}' before_value='{before['value']}' | "
        f"after_label='{after['label']}' after_value='{after['value']}'"
    )

    return {
        "before": before,
        "after": after,
    }


async def scrape_program(page, program: str, trigger_state: str, no_state_filter: bool) -> Tuple[List[dict], dict]:
    mode_label = "no state selected" if no_state_filter else f"trigger state {trigger_state}"
    print(f"Fetching: {program} / {mode_label}")

    await page.goto(AMS_URL, timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)

    prog_select = await find_select_with_programs(page)
    state_select = await find_select_with_states(page)
    country_select, country_value, country_label = await find_country_select_and_value(page)

    if not prog_select or not state_select or not country_select or not country_value:
        raise Exception("Could not locate one or more dropdowns")

    await country_select.select_option(value=country_value)
    await page.wait_for_timeout(1000)
    print(f" Selected country option: {country_label}")

    program_selection_info = await select_dropdown_with_verification(
        prog_select,
        program,
        PROGRAM_SELECTION_WAIT_MS,
        "Program",
    )

    state_selection_info = {
        "before": {"value": "", "label": ""},
        "after": {"value": "", "label": ""},
    }

    if no_state_filter:
        print(" No state selected for this run")
    else:
        state_selection_info = await select_dropdown_with_verification(
            state_select,
            trigger_state,
            STATE_SELECTION_WAIT_MS,
            "State",
        )

    selected_program_before_search = await get_selected_option_info(prog_select)
    selected_state_before_search = await get_selected_option_info(state_select)

    print(
        f" Final selections before search | "
        f"program_label='{selected_program_before_search['label']}' "
        f"program_value='{selected_program_before_search['value']}' | "
        f"state_label='{selected_state_before_search['label']}' "
        f"state_value='{selected_state_before_search['value']}'"
    )

    await page.wait_for_timeout(PRE_SEARCH_WAIT_MS)
    await polite_pause()
    await click_search(page)
    await wait_for_results(page)

    rows, detail_link_count = await scrape_raw_rows(page, program, trigger_state, no_state_filter)

    if detail_link_count == 0 and program in ZERO_RESULT_DEBUG_TARGETS:
        await write_zero_result_debug_artifacts(
            page=page,
            program=program,
            trigger_state=trigger_state,
            no_state_filter=no_state_filter,
            selected_program_before_search=selected_program_before_search,
            selected_state_before_search=selected_state_before_search,
            detail_links_found=detail_link_count,
        )

    unique_states = sorted({r["parsed_state_abbr"] for r in rows if r.get("parsed_state_abbr")})
    sample_names = [r["raw_name_line"] for r in rows[:5] if r.get("raw_name_line")]
    detected_mentions = sorted(
        {
            mention.strip()
            for row in rows
            for mention in (row.get("detected_program_mentions", "") or "").split(",")
            if mention.strip()
        }
    )

    coverage = {
        "program_requested": program,
        "requested_program_unmapped": program not in EXPECTED_PROGRAMS,
        "no_state_filter": no_state_filter,
        "result_scope": get_result_scope(trigger_state, no_state_filter),
        "program_selection_before": program_selection_info["before"],
        "program_selection_after": program_selection_info["after"],
        "state_selection_before": state_selection_info["before"],
        "state_selection_after": state_selection_info["after"],
        "selected_program_before_search": selected_program_before_search,
        "selected_state_before_search": selected_state_before_search,
        "detail_links_found": detail_link_count,
        "rows_parsed": len(rows),
        "unique_parsed_states_count": len(unique_states),
        "unique_parsed_states_sample": unique_states[:15],
        "sample_names": sample_names,
        "detected_program_mentions": detected_mentions,
    }

    print(f" Found {len(rows)} raw rows for {program}")
    print(f" Coverage summary for {program}: {json.dumps(coverage, indent=2)}")

    return rows, coverage


async def run_scrape() -> Tuple[List[dict], List[dict]]:
    all_rows = []
    coverage_summary = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36"
        )
        page = await context.new_page()

        for program in PROGRAMS:
            rows, coverage = await scrape_program(page, program, TRIGGER_STATE, NO_STATE_FILTER)
            all_rows.extend(rows)
            coverage_summary.append(coverage)

            print(f"Total raw rows so far: {len(all_rows)}")

            if LIMIT_LOCATIONS > 0 and len(all_rows) >= LIMIT_LOCATIONS:
                all_rows = all_rows[:LIMIT_LOCATIONS]
                print(f"Global LIMIT_LOCATIONS reached: {LIMIT_LOCATIONS}")
                break

        await browser.close()

    return all_rows, coverage_summary


def print_coverage_report(coverage_summary: List[dict], all_rows: List[dict]):
    print("\n" + "=" * 80)
    print("PROGRAM COVERAGE DIAGNOSTIC REPORT")
    print("=" * 80)

    missing_programs = [c["program_requested"] for c in coverage_summary if c["rows_parsed"] == 0]
    requested_unmapped = [c["program_requested"] for c in coverage_summary if c["requested_program_unmapped"]]
    discovered_unmapped = summarize_unmapped_mentions(all_rows)

    for item in coverage_summary:
        print(json.dumps(item, indent=2))

    print("-" * 80)
    print(f"Programs requested: {PROGRAMS}")
    print(f"NO_STATE_FILTER: {NO_STATE_FILTER}")
    print(f"Programs with zero parsed rows: {missing_programs}")
    print(f"Requested program labels not in canonical list: {requested_unmapped}")
    print(f"Possible unmapped discovered labels: {discovered_unmapped}")
    print(f"Total raw rows captured: {len(all_rows)}")
    print("=" * 80)

    if ENABLE_COVERAGE_DIAGNOSTIC:
        payload = {
            "generated_at_utc": datetime.utcnow().isoformat(),
            "no_state_filter": NO_STATE_FILTER,
            "trigger_state": "" if NO_STATE_FILTER else TRIGGER_STATE,
            "limit_locations": LIMIT_LOCATIONS,
            "programs_requested": PROGRAMS,
            "programs_with_zero_rows": missing_programs,
            "requested_program_labels_unmapped": requested_unmapped,
            "possible_unmapped_discovered_labels": discovered_unmapped,
            "coverage_summary": coverage_summary,
            "total_raw_rows": len(all_rows),
        }

        with open(COVERAGE_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        print(f"Coverage JSON written to: {COVERAGE_JSON_PATH}")


def prep_row_for_supabase(row: dict) -> dict:
    """Prepare a normalized row for the merge_google_sheets_data RPC.

    Field names must match what the RPC reads directly via row_data->>'...'
    (see supabase/migrations/20260101000000_merge_google_sheets_data.sql) —
    provider_name and street_address, never organization/address.
    """
    r = dict(row)
    if isinstance(r.get("services"), list):
        r["services"] = ",".join(r["services"])
    # Ensure geocode_status is present
    if "geocode_status" not in r:
        r["geocode_status"] = "ok" if r.get("latitude") else "pending"
    return r


async def write_to_supabase_direct(normalized_rows: List[dict], batch_size: int = 500) -> dict:
    """Write normalized rows directly to Supabase via the merge_google_sheets_data RPC.

    Bypasses Google Sheets entirely, avoiding the 6-minute Apps Script timeout
    when payloads are large (enriched AJAX + website data).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — skipping direct Supabase write")
        return {}

    rest_url = SUPABASE_URL.rstrip("/") + "/rest/v1/rpc/merge_google_sheets_data"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    prepped = [prep_row_for_supabase(r) for r in normalized_rows]
    totals: dict = {}
    batches = (len(prepped) + batch_size - 1) // batch_size

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(prepped), batch_size):
            batch = prepped[i : i + batch_size]
            batch_num = i // batch_size + 1
            print(f"  Supabase batch {batch_num}/{batches} ({len(batch)} rows)...")
            async with session.post(
                rest_url,
                json={"sheet_data": batch},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise Exception(f"Supabase RPC failed (batch {batch_num}): HTTP {resp.status} — {text[:300]}")
                data = await resp.json()
                for k, v in (data or {}).items():
                    # skipped_details is a list (per-row detail for the
                    # row-issues email), every other field is a plain
                    # count -- list fields concatenate across batches,
                    # numeric fields sum. Numeric '+' on a list would
                    # raise (0 + [...] is a TypeError in Python).
                    if isinstance(v, list):
                        totals[k] = totals.get(k, []) + v
                    else:
                        totals[k] = totals.get(k, 0) + (v or 0)

    return totals


async def notify_row_issues(sb_result: dict, run_metadata: dict) -> None:
    """Email a summary when individual rows had a problem even though the
    overall run succeeded -- a geocode that came back empty (won't appear
    on the map until resolved) or a location the merge RPC had to skip
    (pre-existing duplicate-data conflict). Independent of the Google
    Sheets write path (currently disconnected -- see
    MVP-Rollout-Roadmap.md), unlike the completion/failure emails that
    only fire from inside write_to_google_sheets.
    """
    geocode_failed = sb_result.get("geocode_failed", 0) or 0
    locations_skipped = sb_result.get("locations_skipped", 0) or 0
    if geocode_failed <= 0 and locations_skipped <= 0:
        return
    if not GOOGLE_SHEETS_URL:
        print(f"WARNING: {geocode_failed} geocode failures / {locations_skipped} skipped locations this run, "
              f"but GOOGLE_SHEETS_WEB_APP_URL is not set — cannot email the summary.")
        return

    samples = []
    if os.path.exists("geocode_failures.json"):
        with open("geocode_failures.json", "r", encoding="utf-8") as f:
            samples = json.load(f)

    payload = {
        "action": "notify_row_issues",
        "secret": SCRAPER_SECRET,
        "run_id": run_metadata.get("run_id", "unknown"),
        "github_run_id": os.getenv("GITHUB_RUN_ID", ""),
        "geocode_failed": geocode_failed,
        "locations_skipped": locations_skipped,
        "geocode_failed_samples": samples[:15],
        "skipped_details": (sb_result.get("skipped_details") or [])[:15],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_SHEETS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                print(f"notify_row_issues: HTTP {resp.status} — {text[:200]}")
    except Exception as exc:
        # Never let a notification failure abort an otherwise-successful run.
        print(f"notify_row_issues failed (non-fatal): {exc}")


async def write_to_google_sheets(raw_rows: List[dict], normalized_rows: List[dict], run_metadata: dict):
    payload = {
        "action": "replace_raw_only",
        "test_mode": TEST_MODE,
        "secret": SCRAPER_SECRET,
        "raw_rows": raw_rows,
        "normalized_rows": normalized_rows,
        "run_metadata": run_metadata,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GOOGLE_SHEETS_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            # Was 180s -- confirmed live that writing ~7,400 raw + 7,400
            # normalized rows through Apps Script's SpreadsheetApp (slow for
            # a sheet this large) regularly ran past that. This call is also
            # now wrapped in a non-fatal try/except by its caller (see
            # scraper_main), so a genuinely stuck request still can't hang
            # the job forever -- just gives a real attempt more room to
            # actually finish before giving up.
            timeout=aiohttp.ClientTimeout(total=480),
        ) as response:
            text = await response.text()
            print(f"Google Sheets response status: {response.status}")
            print(f"Google Sheets response body: {text[:500]}")

            if response.status != 200:
                raise Exception(f"Google Sheets write failed with HTTP {response.status}: {text}")


def _load_checkpoint() -> dict | None:
    if not os.path.exists(CHECKPOINT_FILE):
        return None
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            cp = json.load(f)
        age_hours = (time.time() - cp["saved_at_unix"]) / 3600
        if age_hours > CHECKPOINT_MAX_AGE_HOURS:
            print(f"Checkpoint is {age_hours:.1f}h old (max {CHECKPOINT_MAX_AGE_HOURS}h) — ignored, re-scraping")
            return None
        print(f"Checkpoint found ({age_hours:.1f}h old, {len(cp['normalized'])} normalized rows) — skipping scrape phase")
        return cp
    except Exception as exc:
        print(f"Checkpoint load failed: {exc} — re-scraping")
        return None


def _save_checkpoint(all_rows: list, normalized: list, run_metadata: dict, coverage_summary: list) -> None:
    cp = {
        "saved_at_unix": time.time(),
        "all_rows": all_rows,
        "normalized": normalized,
        "run_metadata": run_metadata,
        "coverage_summary": coverage_summary,
    }
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(cp, f)
    print(f"Checkpoint saved: {len(normalized)} normalized rows -> {CHECKPOINT_FILE}")


def _delete_checkpoint() -> None:
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("Checkpoint deleted (run complete)")


async def main():
    print("Starting ACHC raw dump...")
    print(f"TEST_MODE: {TEST_MODE}")
    print(f"PROGRAMS: {PROGRAMS}")
    print(f"TRIGGER_STATE: {TRIGGER_STATE}")
    print(f"NO_STATE_FILTER: {NO_STATE_FILTER}")
    print(f"LIMIT_LOCATIONS: {LIMIT_LOCATIONS}")
    print(f"ENABLE_COVERAGE_DIAGNOSTIC: {ENABLE_COVERAGE_DIAGNOSTIC}")
    print(f"COVERAGE_JSON_PATH: {COVERAGE_JSON_PATH}")
    print(f"ENABLE_ZERO_RESULT_DEBUG: {ENABLE_ZERO_RESULT_DEBUG}")
    print(f"ZERO_RESULT_DEBUG_DIR: {ZERO_RESULT_DEBUG_DIR}")
    print(f"ZERO_RESULT_DEBUG_TARGETS: {ZERO_RESULT_DEBUG_TARGETS}")
    print(f"PROGRAM_SELECTION_WAIT_MS: {PROGRAM_SELECTION_WAIT_MS}")
    print(f"STATE_SELECTION_WAIT_MS: {STATE_SELECTION_WAIT_MS}")
    print(f"PRE_SEARCH_WAIT_MS: {PRE_SEARCH_WAIT_MS}")
    print(f"POST_SEARCH_WAIT_MS: {POST_SEARCH_WAIT_MS}")
    print(f"ENABLE_AJAX_ACCREDITATION: {ENABLE_AJAX_ACCREDITATION}")
    print(f"ENABLE_GEOCODING: {ENABLE_GEOCODING}")
    print(f"ENABLE_WEBSITE_ENRICHMENT: {ENABLE_WEBSITE_ENRICHMENT}")

    # ---------------------------------------------------------------------------
    # Scrape phase — skipped if a fresh checkpoint exists
    # ---------------------------------------------------------------------------
    checkpoint = _load_checkpoint()

    if checkpoint:
        all_rows = checkpoint["all_rows"]
        normalized = checkpoint["normalized"]
        run_metadata = checkpoint["run_metadata"]
        coverage_summary = checkpoint["coverage_summary"]
        print(f"Resumed from checkpoint: {len(all_rows)} raw rows, {len(normalized)} normalized rows")
    else:
        scrape_start_time = time.time()
        scrape_start_dt = datetime.utcnow()

        all_rows, coverage_summary = await run_scrape()

        scrape_end_time = time.time()
        scrape_end_dt = datetime.utcnow()
        duration_seconds = int(scrape_end_time - scrape_start_time)
        duration_minutes = duration_seconds // 60
        duration_remaining_seconds = duration_seconds % 60
        duration_human = f"{duration_minutes} minutes {duration_remaining_seconds} seconds"

        # These programs consistently return 0 results on the ACHC site — not a scrape failure.
        KNOWN_EMPTY_PROGRAMS = {
            "Private Duty",
            "Healthcare Staffing Services Certification",
            "Long-Term Care Dialysis Certification",
            "ACHC Inspection Services",
        }

        programs_with_zero_rows = [
            c["program_requested"] for c in coverage_summary
            if c["rows_parsed"] == 0 and c["program_requested"] not in KNOWN_EMPTY_PROGRAMS
        ]
        scrape_status = "incomplete_scrape" if len(programs_with_zero_rows) > 2 else "complete"

        if programs_with_zero_rows:
            print(f"WARNING: {len(programs_with_zero_rows)} unexpected programs returned 0 rows — marking as incomplete_scrape")
        known_empty_hit = [c["program_requested"] for c in coverage_summary if c["rows_parsed"] == 0 and c["program_requested"] in KNOWN_EMPTY_PROGRAMS]
        if known_empty_hit:
            print(f"Known-empty programs (expected, not a scrape failure): {known_empty_hit}")

        run_metadata = {
            "run_id": f"run_{scrape_start_dt.strftime('%Y%m%dT%H%M%SZ')}",
            "started_at_utc": scrape_start_dt.isoformat(),
            "completed_at_utc": scrape_end_dt.isoformat(),
            "duration_seconds": duration_seconds,
            "duration_human": duration_human,
            "total_rows_captured": len(all_rows),
            "programs_scraped": PROGRAMS,
            "programs_with_zero_rows": programs_with_zero_rows,
            "limit_locations": LIMIT_LOCATIONS,
            "no_state_filter": NO_STATE_FILTER,
            "trigger_state": "" if NO_STATE_FILTER else TRIGGER_STATE,
            "test_mode": TEST_MODE,
            "scrape_status": scrape_status,
            "ajax_accreditation_enabled": ENABLE_AJAX_ACCREDITATION,
        }

        print(f"Scrape duration: {duration_human}")
        print(f"Scrape status: {scrape_status}")

        if not all_rows and not TEST_MODE:
            raise Exception("No rows scraped")

        print(f"Raw rows captured: {len(all_rows)}")
        print("Sample raw rows:")
        for row in all_rows[:3]:
            print(
                {
                    "raw_index": row["raw_index"],
                    "searched_program_type": row["searched_program_type"],
                    "search_trigger_state": row["search_trigger_state"],
                    "parsed_state_abbr": row["parsed_state_abbr"],
                    "matches_trigger_state": row["matches_trigger_state"],
                    "detected_program_mentions": row["detected_program_mentions"],
                    "raw_name_line": row["raw_name_line"],
                    "result_scope": row["result_scope"],
                }
            )

        print_coverage_report(coverage_summary, all_rows)

        normalized = normalize_rows(all_rows)
        print(f"Normalized rows: {len(normalized)}")

        _save_checkpoint(all_rows, normalized, run_metadata, coverage_summary)

    # ---------------------------------------------------------------------------
    # Enrichment phase — geocode, websites, AJAX (runs whether fresh or resumed)
    # ---------------------------------------------------------------------------
    if ENABLE_GEOCODING:
        normalized = await geocode_locations(normalized)
        # Opt-in second pass (ENABLE_NOMINATIM_FALLBACK) for whatever's
        # still "pending" after the Google budget-capped run above -- see
        # geocode_helper.py for why this is deliberately off by default
        # and sequential/slow. No-ops immediately if the flag isn't set.
        normalized = await geocode_locations_nominatim_fallback(normalized)
        geocode_failures = [r for r in normalized if r.get("geocode_status") == "failed"]
        geocode_ok       = [r for r in normalized if r.get("geocode_status") == "ok"]
        print(f"Geocoding complete: {len(geocode_ok)} with coords, {len(geocode_failures)} without")
        if geocode_failures:
            run_metadata["geocode_failures"] = len(geocode_failures)
            run_metadata["geocode_failures_note"] = "Enable billing at console.cloud.google.com/project/_/billing/enable to resolve. See geocode_failures.json for the full list."
    else:
        print("Geocoding disabled (ENABLE_GEOCODING=false) — dedup report only, no API calls:")
        print_dedup_report(normalized)

    if ENABLE_WEBSITE_ENRICHMENT:
        normalized = await enrich_websites(normalized)
        print(f"Website enrichment complete")
    else:
        print("Website enrichment disabled (ENABLE_WEBSITE_ENRICHMENT=false)")

    if ENABLE_AJAX_ACCREDITATION:
        company_ids = [r["achc_company_id"] for r in normalized if r.get("achc_company_id")]
        if company_ids:
            ajax_results = await fetch_accreditation_services(company_ids)
            normalized = merge_ajax_services(normalized, ajax_results)
            print(f"AJAX accreditation enrichment complete: {len(company_ids)} providers queried")
        else:
            print("AJAX accreditation skipped: no company IDs available")
    else:
        print("AJAX accreditation disabled (ENABLE_AJAX_ACCREDITATION=false)")

    dump_path = os.getenv("DUMP_NORMALIZED_JSON_PATH", "").strip()
    if dump_path:
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
        print(f"Dumped {len(normalized)} normalized rows to {dump_path} (DUMP_NORMALIZED_JSON_PATH)")

    # Write enriched normalized rows directly to Supabase (bypasses Google Sheets
    # 6-minute timeout that triggers when AJAX services + website data is included).
    if ENABLE_DIRECT_SUPABASE:
        print("Writing normalized rows directly to Supabase...")
        sb_result = await write_to_supabase_direct(normalized)
        print(f"Supabase direct write complete: {sb_result}")
        await notify_row_issues(sb_result, run_metadata)

        # Hide published listings ACHC no longer lists (pending review), only
        # after a complete national scrape has been written -- see
        # retire_unseen.py for the safety rails. Non-fatal: a problem here
        # must never mark an otherwise-good data refresh as failed.
        if ENABLE_RETIRE_UNSEEN:
            try:
                retire_result = await retire_unseen_locations(
                    normalized, run_metadata, dry_run=RETIRE_UNSEEN_DRY_RUN,
                )
                print(f"Retire-unseen result: {retire_result}")
            except Exception as exc:
                print(f"Retire-unseen failed (non-fatal, listings unchanged where not yet patched): {exc!r}")
    else:
        print("Direct Supabase write disabled (ENABLE_DIRECT_SUPABASE=false)")

    # Google Sheets: read-only backup/audit trail only (see daily-scraper.yml)
    # -- production reads from Supabase directly (write_to_supabase_direct
    # above), which has already succeeded by this point. Never let a slow or
    # failed Sheets write fail the whole run: confirmed live that writing
    # ~7,400 raw + 7,400 normalized rows via the Apps Script endpoint
    # regularly ran past write_to_google_sheets' client timeout, raising
    # asyncio.TimeoutError (an empty exception message, hence the blank
    # "ERROR:" line) and failing every nightly run for the past several days
    # even though the actual Supabase write it happened after had already
    # succeeded -- also silently skipping _delete_checkpoint() and the "Run
    # complete" line below every single time.
    if GOOGLE_SHEETS_URL:
        try:
            await write_to_google_sheets(all_rows, normalized, run_metadata)
            print("Raw and normalized data written to Google Sheets")
        except Exception as exc:
            print(f"Google Sheets write failed (non-fatal — Supabase write above is what the live site reads): {exc!r}")
    else:
        print("Skipping Google Sheets write — GOOGLE_SHEETS_WEB_APP_URL is not set")
    _delete_checkpoint()
    print(f"Run complete: {run_metadata['run_id']} — {run_metadata['duration_human']} — {len(all_rows)} rows — status: {run_metadata['scrape_status']}")


if __name__ == "__main__":
    import sys as _sys

    # Mirror stdout to scraper_run.log so we never lose output to a broken pipe.
    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, data):
            for s in self._streams:
                s.write(data)
        def flush(self):
            for s in self._streams:
                s.flush()
        def __getattr__(self, name):
            return getattr(self._streams[0], name)

    import datetime as _dt
    _log = None
    for _log_path in ("scraper_run.log", f"scraper_run_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"):
        try:
            _log = open(_log_path, "w", encoding="utf-8", buffering=1)
            print(f"Logging to {_log_path}", file=_sys.__stdout__)
            break
        except PermissionError:
            print(f"Warning: {_log_path} is locked, trying fallback", file=_sys.__stdout__)

    if _log:
        _sys.stdout = _Tee(_sys.__stdout__, _log)
        _sys.stderr = _Tee(_sys.__stderr__, _log)

    try:
        asyncio.run(main())
    finally:
        if _log:
            _log.flush()
            _log.close()
