import asyncio
import random
import re
from typing import Dict, List

import aiohttp

CANONICAL_PROGRAMS = [
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

_CANONICAL_LOWER = {p.lower(): p for p in CANONICAL_PROGRAMS}

_AJAX_URL = "https://ams.achc.org/accredited_organizations.aspx/GetAccreditedServicesListByCompanyID"
_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
}


# ACHC's GetAccreditedServicesListByCompanyID response is one or more
# accreditation periods, each shaped like:
#   <p><strong>Dates:</strong> 10/21/2025 through 10/21/2028</p>
#   <p><strong>Program:</strong> Home Health (Deemed)</p>
#   <p><strong>Services:</strong> Home Health Aide Services, Skilled Nursing ...</p>
# The program name is the text AFTER the bold "Program:" label, not inside
# the bold tag. The previous parser only read text inside <strong>/<b>
# tags, so it only ever saw the labels ("Dates:", "Program:",
# "Services:"), never matched a program, and returned [] for every
# provider (confirmed live 2026-11-11 against 7,370 providers). The
# Services line is deliberately ignored: "Home Care Aide Services" is a
# service under a program, not evidence of the Home Care program itself.
_PROGRAM_LABEL_RE = re.compile(
    r"<(?:strong|b)[^>]*>\s*Program\s*:?\s*</(?:strong|b)>\s*:?\s*([^<]+)",
    flags=re.IGNORECASE,
)
# Longest names first, so a more specific program can't be shadowed by a
# shorter one it happens to contain.
_CANONICAL_BY_LENGTH = sorted(_CANONICAL_LOWER.items(), key=lambda kv: -len(kv[0]))


def canonical_program(program_text: str) -> str:
    """Map one ACHC 'Program:' value to a canonical program name, or ''.

    Drops ACHC's qualifiers in parentheses, e.g. "Home Health (Deemed)" or
    "Hospice (Deemed under parent provider number)".
    """
    text = re.sub(r"\([^)]*\)", " ", program_text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    if not text:
        return ""
    if text in _CANONICAL_LOWER:
        return _CANONICAL_LOWER[text]
    for key, canonical in _CANONICAL_BY_LENGTH:
        if text.startswith(key):
            return canonical
    return ""


def parse_accreditation_html(html: str) -> List[str]:
    """Return the sorted canonical programs listed in an ACHC accreditation response."""
    if not html:
        return []
    found = set()
    for raw in _PROGRAM_LABEL_RE.findall(html):
        canonical = canonical_program(raw)
        if canonical:
            found.add(canonical)
    return sorted(found)


async def _fetch_one(
    session: aiohttp.ClientSession,
    company_id: int,
    delay_range: tuple,
    max_retries: int,
) -> List[str]:
    body = {"lngCompanyID": company_id, "boolFullDescription": True}

    for attempt in range(max_retries + 1):
        try:
            async with session.post(_AJAX_URL, json=body, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
                html = data.get("d", "") or ""
                return parse_accreditation_html(html)
        except Exception as exc:
            if attempt < max_retries:
                await asyncio.sleep(1.0)
            else:
                print(f"  WARNING: accreditation fetch failed for company_id={company_id}: {exc}")
                return []

    return []


async def fetch_accreditation_services(
    company_ids: List,
    base_url: str = _AJAX_URL,
    delay_range: tuple = (0.15, 0.35),
    max_retries: int = 2,
    concurrency: int = 10,
) -> Dict[str, List[str]]:
    int_ids = []
    for cid in company_ids:
        try:
            int_ids.append(int(cid))
        except (TypeError, ValueError):
            print(f"  WARNING: skipping invalid company_id: {cid!r}")

    print(f"Fetching accreditation details for {len(int_ids)} providers via AJAX (concurrency={concurrency})...")
    results: Dict[str, List[str]] = {}
    sem = asyncio.Semaphore(concurrency)
    completed = 0

    async def _fetch_with_delay(session: aiohttp.ClientSession, cid: int) -> None:
        nonlocal completed
        async with sem:
            services = await _fetch_one(session, cid, delay_range, max_retries)
            await asyncio.sleep(random.uniform(*delay_range))
        results[str(cid)] = services
        completed += 1
        if completed % 500 == 0:
            print(f"  ...{completed}/{len(int_ids)} fetched")

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[_fetch_with_delay(session, cid) for cid in int_ids])

    print(f"Accreditation fetch complete: {len(results)} results")
    return results


def merge_ajax_services(
    normalized_rows: List[dict],
    ajax_results: Dict[str, List[str]],
) -> List[dict]:
    for row in normalized_rows:
        cid = str(row.get("achc_company_id", "") or "")
        if not cid:
            continue
        ajax_services = ajax_results.get(cid)
        if ajax_services:
            row["services"] = ajax_services
    return normalized_rows
