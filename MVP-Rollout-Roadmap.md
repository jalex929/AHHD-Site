# MVP Rollout Roadmap

_Created 2026-07-31. Tracks the staged rollout of the ACHC scraper -> Supabase pipeline, paced against Google Maps Platform's free tiers so no step costs money without an explicit decision to spend it._

Note that much of the dev is done in Claude Code but UI stuff will be done in Bolt. copy edits may also happen here.

## Why this exists

Billing was only just enabled on the Google Cloud project behind `GOOGLE_MAPS_API_KEY`. Rather than run the full pipeline (geocoding + website enrichment) against the entire ACHC dataset in one shot, we're rolling out in stages: confirm real numbers with zero-cost diagnostic passes first, geocode only what fits inside the free tier, verify the caching/dedup logic actually prevents re-billing, and only then decide whether to expand scope or spend real money.

## Confirmed numbers (from account-specific pricing export + live diagnostic runs)

| API | Free tier (this account) | Price beyond free tier |
|---|---|---|
| Geocoding API | 10,000 calls/month | $5.00/1000 (0-100K), stepping down at higher volume |
| Places API (New) Text Search, Enterprise tier (`websiteUri` field) | 1,000 calls/month | $35.00/1000 (0-100K) — most expensive call in the pipeline |

| Scope | Raw rows | Normalized providers | Unique addresses |
|---|---|---|---|
| **MVP: Home Care + Home Health + Hospice** | 7,452 | 7,416 | **7,308** |
| Full: all 20 ACHC program types | 23,645 | 22,526 | 22,244 |

**MVP scope (7,308 unique addresses) fits entirely inside the 10,000/month free geocoding tier — a single pass costs $0.** Full national scope would take ~3 months of free-tier budget to fully geocode incrementally without ever paying (22,244 / ~9,500 per month), or less if some months allow spending beyond free.

The ~4,600-provider figure in older repo notes (`context.md`) is stale — it predates the DEFAULT_PROGRAMS expansion to 20 program types (T-014).

## Mechanisms already built (as of 2026-07-31)

- **Address-level dedup before geocoding** (`geocode_helper.py: group_by_address`) — an institution listed once per accredited program/service is geocoded exactly once, not once per listing. Confirmed via `print_dedup_report()` in every run's log.
- **Cross-run geocode cache** (`geocode_cache.json`, persisted via `actions/cache` in `daily-scraper.yml`) — an address that's already been geocoded is never re-billed on a later run.
- **Explicit monthly budget tracker** (`geocode_monthly_budget.json`, also persisted via `actions/cache`) — counts actual API calls made in the current calendar month (`GEOCODE_MONTHLY_BUDGET`, default 9,500 — a safety margin under Google's 10,000 free). Once the monthly budget is spent, new geocode calls stop automatically and resume when the month rolls over. This is deliberately calendar-month based to match Google's actual reset cadence, not an approximation via run frequency.
- **`ENABLE_GEOCODING` / `ENABLE_WEBSITE_ENRICHMENT` / `ENABLE_DIRECT_SUPABASE` toggles** (`achc_scraper.py`) — each write/spend path can be independently switched off for a zero-cost, report-only diagnostic run.
- **`TEST_PROGRAMS` env override** — scopes a run to a specific subset of ACHC program types (used to isolate the MVP set above) without touching `DEFAULT_PROGRAMS`.

## Fallback if the Geocoding API rate/budget limit is ever hit

Before the Google Maps Geocoding API rewrite (see `AHHD-Production-Synchronization-Strategy.md`, P0-001), `geocode_helper.py` used **Nominatim** (OpenStreetMap's free geocoder). It's genuinely free and has no monthly cap, but its usage policy limits requests to 1/second with no bulk parallelism — geocoding the full 22,244-address backlog at that rate would take over 6 hours straight, versus minutes with Google's concurrent requests. That's why it was replaced.

Resurfacing it as a fallback, not a replacement: if the Google Geocoding monthly budget is ever exhausted and a run needs coordinates sooner than "wait for next month," Nominatim could be wired back in as a secondary path — e.g., only for addresses still `pending` after the Google budget caps out for the month, run slowly in the background against Nominatim's 1 req/sec limit. Not implemented; noting it here so it doesn't need to be rediscovered later.

## Rollout phases

### Phase 0 — Scope confirmation (done, 2026-07-31)
Zero-cost diagnostic passes confirmed the MVP scope (7,308 unique addresses) and full scope (22,244) numbers above. No geocoding, no Supabase writes, no Sheets writes.

### Phase 1 — MVP geocoding (next)
Run the MVP scope (Home Care, Home Health, Hospice) for real: `ENABLE_GEOCODING=true`, `ENABLE_DIRECT_SUPABASE=true`, `TEST_PROGRAMS=Home Care,Home Health,Hospice`, website enrichment off, Sheets still disconnected. Cost: $0 (7,308 < 9,500/month budget, single pass). This populates Supabase with real, geocoded MVP-scope data for the first time.

### Phase 2 — Verify no unnecessary re-geocoding
Re-run the same scope shortly after Phase 1 completes. Expected: `Address dedup` report shows the same ~7,308 unique addresses, but the geocode step reports ~7,308 "served from cache" and 0 new API calls. This is the concrete test that cache persistence + the monthly budget tracker are both working end-to-end, not just in theory.

### Phase 3 — Rewire the nightly cron to the MVP scope
Once Phase 1/2 are verified, `daily-scraper.yml`'s schedule-triggered run should use the MVP `TEST_PROGRAMS` scope (or `DEFAULT_PROGRAMS` narrowed to the MVP set directly) as the new steady-state nightly job — geocoding on, direct Supabase writes on. Because of the cache + monthly budget mechanism, nightly runs after the initial backfill will only spend budget on genuinely new/changed addresses, which should be a small trickle, not a repeat of the full 7,308.

### Phase 4 — Site/UI work (parallel or after Phase 3)
Shift focus to the actual site: fix UI issues, verify search functionality, get content ready to be live, connect the custom domain, and move off Wix hosting. This is a separate workstream from the data pipeline and hasn't been scoped yet — needs its own investigation pass into the current site codebase before planning.

### Phase 5 — Expand scope beyond MVP (future, deliberate)
Once the MVP is live and stable, incrementally add more ACHC program types back toward the full 20-program set. Each expansion adds new unique addresses to the geocode backlog; the monthly budget tracker will trickle those in automatically over subsequent nightly runs without needing a lump-sum payment, unless a faster rollout is explicitly wanted (in which case, paying to geocode the backlog immediately is a deliberate spend decision, not an accident).

**Which programs to add and when is a product decision, not something to automate on a timer.** Scope expansion should stay a manual, deliberate change (edit `TEST_PROGRAMS`/`DEFAULT_PROGRAMS`, review the new dedup numbers via a zero-cost diagnostic pass first, then push).

## Can this run unattended via cron, or does it need manual triggering?

**Yes, for the geocoding budget mechanism specifically** — once a program scope is set (Phase 3), the existing nightly cron (`0 6 * * *` UTC) can run indefinitely without manual intervention:
- It will only spend geocode budget on new/changed addresses each night.
- It self-throttles to stay under the monthly free tier automatically (via `geocode_monthly_budget.json`).
- It resumes automatically when the calendar month rolls over.

**No, for scope expansion (Phase 5)** — deciding to add more ACHC program types is a deliberate product/content decision and is intentionally NOT automated on a timer. That should stay a manual step: run a zero-cost diagnostic first to see the new numbers, then explicitly commit the scope change.

**No, for Places API / website enrichment** — per a standing project rule, any change that wires up or expands Places API usage requires an explicit warning and **two separate confirmations** from Jay before it's applied, given it's the most expensive call in the pipeline ($35/1000, only 1,000/month free). This is documented in Claude's persistent memory for this project so it's enforced across sessions, not just this conversation.

## Decided: provider website discovery via search deep link

Instead of pre-fetching website URLs via a paid API, each provider page gets a link that launches a web search built from the information we actually have on file for that location (name + address) — the site doesn't claim to know or verify the provider's website itself, and the visitor is expected to confirm the results they see match before relying on them.

**Validation:** `website_search_url` generated for all 7,416 real MVP-scope providers (`data/normalized_with_search_links.json`). Tested via:
- 9-record manual pilot (`website-search-test-links.md` / `website-search-test-results-claude.md`) — established the corroboration methodology (NPI number > exact address > phone > Google Knowledge Panel > name similarity alone) after catching a real miss in our own first-pass tooling (a DBA-name mismatch, confirmed via a live screenshot).
- 250-record random sample (seed `20260731`), tested in parallel via 10 subagents using that methodology — 135/250 completed before the session's shared WebSearch budget (200 calls/session, shared across all agents) was exhausted; remaining batches cancelled rather than run in a degraded state. Results (`website-search-test-results-250sample.md`): **83.7% confirmed match, 15.6% uncertain (flagged, not failures), 0.7% no match found** on n=135.

**Disclaimer copy for the site** (final): _"This search is launched using the information we have on file for this location. Please confirm the results you see match before relying on them — details can change over time, and some organizations operate under a different name (a "doing business as" name) than the legal entity name on file with their accrediting body."_

## Decided: listings must match ACHC's current list (2026-11-11)

Traced and fixed in PRs #1 and #2 (see their descriptions for the evidence):
- **Program parser:** ACHC's accreditation lookup lists the program after a bold `Program:` label. The old parser read only the bold text, so it returned nothing for every provider and 1,523 accredited providers were hidden. Now 100% of scraped providers get a care type.
- **Not listed by ACHC → unpublished.** If ACHC stops listing a provider, it's treated as no longer accredited: `listing_status='unpublished'`, nothing deleted, republished automatically if ACHC lists it again (676 unpublished on the first run).
- **Listed by ACHC, care type unknown → review queue** (`needs_review` + review item).
- **Orphan duplicates removed:** 578 copies created by HTML-encoded names (`&amp;`) before 2026-05-31, with a JSON backup kept locally (`../data/duplicate-cleanup/backups/`).
- **Nightly content health check** (workflow step): care-type coverage, no stale published listings, accredited providers not missing, frozen-listing warning.
- Live count after the fixes: 7,299 (2026-11-12).

## Decided: review queue shows the source and gates approval on site-ready checks (2026-11-13)

**Rule (Jay):** a listing in the Review Queue can be approved only if it passes every site-ready check. For now, anything that did not come from ACHC's own accredited list cannot be approved, because nothing independently confirms its accreditation yet.

What was built:
- **Source tags** on `locations.source_system`: `achc` (nightly scrape; also the column's database default) and `admin_manual` (the admin Add New Listing wizard now sets this explicitly). Before this, hand-entered listings were silently tagged `achc`.
- **Site-ready checks** (`src/lib/siteReadiness.ts`, tests in `siteReadiness.test.ts`): accreditation confirmed by ACHC (`source_system='achc'` **and** an ACHC accreditation ID), clean provider name (no `&amp;`), complete address, map coordinates, and a care type the site shows.
- **Review Queue page:** shows a source badge and the checklist on each item; **Approve & Publish is disabled** until every check passes, and the approve handler re-checks before publishing.

The one item in the queue on 2026-11-13 (1st Choice Home Health Care & Hospice, Oklahoma City) is tagged `achc` but has no ACHC ID (a May import), so it correctly can't be approved.

## Next steps (build backlog)

Review queue and sources:
- **Auto-publish** items that pass every check and come from ACHC, so a person only sees the ones that genuinely need judgment.
- **A way to verify manual entries** (e.g. look the provider up on ACHC's site by name and address, and attach the ACHC ID if found), so a manual entry can become approvable instead of permanently blocked.
- Add source tags for any future intake paths (public "Get Listed" submissions, bulk imports) and show them in Manage Listings, not only the queue.
- Backfill: every existing row is `achc` by default. Rows with no `achc_source_id` are the ones whose real origin is unknown; review them.

Data and admin cleanup (found while documenting the admin, 2026-11-13):
- **Empty `&amp;` organizations** still appear in admin › Organizations next to their clean twins (left over from the duplicate cleanup). Remove organizations that have no locations.
- **36 merge skips a night / 18 frozen listings:** 4 are one provider holding two ACHC IDs at one address; 32 unexplained. Investigate.
- **Edit Listing** reopens the wizard at the Google Maps search step instead of showing the listing's current details.
- **Images, Content, and Search Analytics** admin pages are placeholders (no functionality yet).

Tooling:
- **ESLint can't parse TypeScript** anywhere in this repo (`Parsing error` on `interface` / `import type`), so lint gives no signal. Fix the parser config.

## Open questions / not yet decided
- Whether to reconnect Google Sheets as an audit trail once the MVP data is stable in Supabase (currently disconnected — no completion/failure emails fire while it's off).
- Whether to run the remaining ~115 untested records from the 250-sample (would need a fresh session or a raised WebSearch budget) — the completed 135 may already be sufficient signal.
- Exact site/UI/search-functionality scope for Phase 4 — not yet investigated, including the provider detail-page requirement (click a listing -> land on a page with that provider's full info) for the Bolt build.
- 2026-08-15: idea for a future enrichment pass — periodically (not just at first import) re-pull provider details from Google Search/Google Business listings, both scripted where possible and manually where it isn't, to catch drift in critical fields (phone number, address) against what ACHC has on file. Would reuse the same confirmed/uncertain/no-match confidence classification already built and tested for the website-search-deep-link matching above (n=135, seed `20260731`) rather than a new one-off scoring scheme. Worth a look at existing open-source scraping/enrichment tools before building this from scratch. Any path that touches the Places API specifically is still subject to the standing double-confirmation requirement (see the ENABLE_WEBSITE_ENRICHMENT note in daily-scraper.yml) — not automatically in scope just because this idea is logged.
