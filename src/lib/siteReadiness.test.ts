import { describe, expect, it } from 'vitest';
import { checkSiteReadiness, sourceLabel, type ReadinessInput } from './siteReadiness';

const ready: ReadinessInput = {
  source_system: 'achc',
  achc_source_id: '96311',
  organization_name: 'Day Light Healthcare Inc',
  address_line_1: '100 Main St',
  city: 'Springfield',
  state: 'IL',
  postal_code: '62701',
  latitude: 39.8,
  longitude: -89.65,
  service_type_slugs: ['home-health-care'],
};

const failed = (input: ReadinessInput) =>
  checkSiteReadiness(input).checks.filter((c) => !c.passed).map((c) => c.id);

describe('checkSiteReadiness', () => {
  it('passes a complete listing from ACHC', () => {
    expect(checkSiteReadiness(ready).ready).toBe(true);
  });

  it('never approves a manually added listing, even if everything else is perfect', () => {
    const result = checkSiteReadiness({ ...ready, source_system: 'admin_manual', achc_source_id: null });
    expect(result.ready).toBe(false);
    expect(failed({ ...ready, source_system: 'admin_manual', achc_source_id: null })).toEqual(['accreditation']);
    expect(result.checks[0].detail).toMatch(/Manually added listings can't be approved yet/);
  });

  it('does not treat an ACHC-tagged row without an ACHC ID as verified', () => {
    // The real queued item on 2026-11-13: a May import tagged 'achc' with no ID.
    expect(failed({ ...ready, achc_source_id: null })).toEqual(['accreditation']);
  });

  it('flags HTML-encoded names from the old imports', () => {
    expect(failed({ ...ready, organization_name: '1 &amp; 1 Hospice, Inc.' })).toEqual(['name']);
  });

  it('requires a complete address', () => {
    expect(failed({ ...ready, postal_code: '627' })).toEqual(['address']);
    expect(failed({ ...ready, state: 'Illinois' })).toEqual(['address']);
    expect(failed({ ...ready, address_line_1: ' ' })).toEqual(['address']);
    expect(checkSiteReadiness({ ...ready, postal_code: '62701-1234' }).ready).toBe(true);
  });

  it('requires map coordinates', () => {
    expect(failed({ ...ready, latitude: null })).toEqual(['map']);
  });

  it('requires a care type the site actually shows', () => {
    expect(failed({ ...ready, service_type_slugs: [] })).toEqual(['care_type']);
    expect(failed({ ...ready, service_type_slugs: ['pharmacy'] })).toEqual(['care_type']);
  });

  it('reports every failing check, not just the first', () => {
    expect(failed({ source_system: 'admin_manual' })).toEqual(['accreditation', 'name', 'address', 'map', 'care_type']);
  });
});

describe('sourceLabel', () => {
  it('names each source plainly', () => {
    expect(sourceLabel('achc', '96311')).toBe('ACHC website (nightly scrape)');
    expect(sourceLabel('achc', null)).toBe('ACHC website (nightly scrape), no ACHC ID on record');
    expect(sourceLabel('admin_manual')).toBe('Added manually in admin');
    expect(sourceLabel(null)).toBe('Unknown source');
    expect(sourceLabel('import_csv')).toBe('Other (import_csv)');
  });
});
