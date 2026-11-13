/**
 * Site-readiness checks for listings in the admin Review Queue.
 *
 * Rule (Jay, 2026-11-13): a listing can be approved only if it passes every
 * check. For now, anything that did NOT come from ACHC's own accredited list
 * (e.g. added by hand in the admin wizard) cannot be approved: there's no
 * independent confirmation of its accreditation yet.
 *
 * Source tags (locations.source_system):
 *   'achc'          -- nightly ACHC scrape (the column's database default)
 *   'admin_manual'  -- added by hand in the admin Add New Listing wizard
 * The database default is 'achc', so the wizard sets 'admin_manual'
 * explicitly. A row only counts as ACHC-verified when it ALSO carries an
 * ACHC accreditation ID (achc_source_id): early May-2026 imports were
 * tagged 'achc' before IDs were captured.
 */

export const SHOWN_SERVICE_SLUGS = ['home-care', 'home-health-care', 'hospice'] as const;

export interface ReadinessInput {
  source_system?: string | null;
  achc_source_id?: string | null;
  organization_name?: string | null;
  address_line_1?: string | null;
  city?: string | null;
  state?: string | null;
  postal_code?: string | null;
  latitude?: number | string | null;
  longitude?: number | string | null;
  service_type_slugs?: string[];
}

export interface ReadinessCheck {
  id: 'accreditation' | 'name' | 'address' | 'map' | 'care_type';
  label: string;
  passed: boolean;
  detail: string;
}

export interface ReadinessResult {
  ready: boolean;
  checks: ReadinessCheck[];
}

const SOURCE_LABELS: Record<string, string> = {
  achc: 'ACHC website (nightly scrape)',
  admin_manual: 'Added manually in admin',
};

export function sourceLabel(source: string | null | undefined, achcSourceId?: string | null): string {
  if (!source) return 'Unknown source';
  const base = SOURCE_LABELS[source] ?? `Other (${source})`;
  return source === 'achc' && !achcSourceId ? `${base}, no ACHC ID on record` : base;
}

const blank = (v: unknown) => v === null || v === undefined || String(v).trim() === '';
const hasHtmlEntity = (s: string) => /&(amp|#\d+|#x[0-9a-f]+|[a-z]+);/i.test(s);

export function checkSiteReadiness(loc: ReadinessInput): ReadinessResult {
  const name = (loc.organization_name ?? '').trim();
  const fromAchc = loc.source_system === 'achc' && !blank(loc.achc_source_id);
  const slugs = loc.service_type_slugs ?? [];
  const shown = slugs.filter((s) => (SHOWN_SERVICE_SLUGS as readonly string[]).includes(s));
  const lat = Number(loc.latitude);
  const lng = Number(loc.longitude);

  const checks: ReadinessCheck[] = [
    {
      id: 'accreditation',
      label: 'Accreditation confirmed by ACHC',
      passed: fromAchc,
      detail: fromAchc
        ? `On ACHC's accredited list (ID ${loc.achc_source_id})`
        : loc.source_system === 'achc'
          ? 'Tagged as ACHC data but has no ACHC accreditation ID, so it can\'t be confirmed'
          : 'Not from ACHC\'s accredited list. Manually added listings can\'t be approved yet',
    },
    {
      id: 'name',
      label: 'Provider name is clean',
      passed: name.length > 0 && !hasHtmlEntity(name),
      detail: !name ? 'Missing provider name' : hasHtmlEntity(name) ? `Name contains encoded text: "${name}"` : 'OK',
    },
    {
      id: 'address',
      label: 'Complete address',
      passed:
        !blank(loc.address_line_1) && !blank(loc.city) &&
        /^[A-Z]{2}$/.test(String(loc.state ?? '').trim()) &&
        /^\d{5}(-\d{4})?$/.test(String(loc.postal_code ?? '').trim()),
      detail: 'Street, city, 2-letter state, and 5-digit ZIP',
    },
    {
      id: 'map',
      label: 'Shows on the map',
      passed: Number.isFinite(lat) && Number.isFinite(lng) && !blank(loc.latitude) && !blank(loc.longitude),
      detail: 'Needs map coordinates (geocoded address)',
    },
    {
      id: 'care_type',
      label: 'Has a care type the site shows',
      passed: shown.length > 0,
      detail: shown.length > 0 ? shown.join(', ') : 'Needs Home Care, Home Health, or Hospice',
    },
  ];

  return { ready: checks.every((c) => c.passed), checks };
}
