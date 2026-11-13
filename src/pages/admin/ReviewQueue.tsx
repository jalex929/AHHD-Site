import { AlertCircle, LogOut, CheckCircle2, XCircle, ExternalLink } from 'lucide-react';
import { useAuth } from '../../contexts/AuthContext';
import { supabase } from '../../lib/supabase';
import { useState, useEffect } from 'react';
import type { LocationWithDetails } from '../../types/database';
import { checkSiteReadiness, sourceLabel } from '../../lib/siteReadiness';

const readinessFor = (location: LocationWithDetails) =>
  checkSiteReadiness({
    source_system: location.source_system,
    achc_source_id: location.achc_source_id,
    organization_name: location.organization?.organization_name,
    address_line_1: location.address_line_1,
    city: location.city,
    state: location.state,
    postal_code: location.postal_code,
    latitude: location.latitude,
    longitude: location.longitude,
    service_type_slugs: (location.service_types || []).map((st) => st.service_type_slug || '').filter(Boolean),
  });

export default function ReviewQueue() {
  const { signOut } = useAuth();
  const [locations, setLocations] = useState<LocationWithDetails[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadReviewQueue();
  }, []);

  const loadReviewQueue = async () => {
    setLoading(true);
    try {
      const { data, error } = await supabase
        .from('locations')
        .select(`
          *,
          organization:organizations(*),
          service_types:location_service_types(
            service_type:service_types(*)
          )
        `)
        .eq('listing_status', 'needs_review')
        .order('created_at', { ascending: false });

      if (error) throw error;

      const mapped = data?.map((loc: any) => ({
        ...loc,
        service_types: loc.service_types?.map((st: any) => st.service_type).filter(Boolean) || []
      })) || [];

      setLocations(mapped);
    } catch (error) {
      console.error('Error loading review queue:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (locationId: string) => {
    // Re-check here too, so the rule holds even if the button state is stale.
    const location = locations.find((l) => l.location_id === locationId);
    if (!location || !readinessFor(location).ready) return;
    try {
      const { error } = await supabase
        .from('locations')
        .update({ listing_status: 'published' })
        .eq('location_id', locationId);

      if (error) throw error;
      loadReviewQueue();
    } catch (error) {
      console.error('Error approving listing:', error);
      alert('Failed to approve listing');
    }
  };

  const handleReject = async (locationId: string) => {
    const reason = prompt('Enter reason for rejection (optional):');
    try {
      const { error } = await supabase
        .from('locations')
        .update({
          listing_status: 'draft',
          admin_notes: reason || null
        })
        .eq('location_id', locationId);

      if (error) throw error;
      loadReviewQueue();
    } catch (error) {
      console.error('Error rejecting listing:', error);
      alert('Failed to reject listing');
    }
  };

  const handleSignOut = async () => {
    await signOut();
    window.location.href = '/admin';
  };

  return (
    <div className="min-h-screen bg-neutral-50">
      <header className="bg-navy-800 text-white shadow-lg">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-heading font-bold text-white">Review Queue</h1>
              <p className="text-primary-300 mt-1">
                {loading ? 'Loading...' : `${locations.length} ${locations.length === 1 ? 'item' : 'items'} needing review`}
              </p>
            </div>
            <div className="flex gap-3">
              <a href="/admin/dashboard" className="bg-neutral-700 text-white hover:bg-neutral-600 px-4 py-2 rounded-lg font-medium transition-colors">
                Back to Dashboard
              </a>
              <button
                onClick={handleSignOut}
                className="bg-slate-700 text-white hover:bg-slate-600 px-4 py-2 rounded-lg font-medium transition-colors flex items-center gap-2"
              >
                <LogOut className="w-4 h-4" aria-hidden="true" />
                Sign Out
              </button>
            </div>
          </div>
        </div>
      </header>

      <main id="main-content" className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="inline-block animate-spin rounded-full h-12 w-12 border-4 border-primary-200 border-t-primary-600"></div>
          </div>
        ) : locations.length === 0 ? (
          <div className="card p-12 text-center">
            <AlertCircle className="w-16 h-16 text-neutral-400 mx-auto mb-4" aria-hidden="true" />
            <h2 className="text-xl font-heading font-semibold text-navy-800 mb-2">No Items to Review</h2>
            <p className="text-neutral-600">All listings are up to date</p>
          </div>
        ) : (
          <div className="space-y-6">
            {locations.map((location) => {
              const readiness = readinessFor(location);
              return (
              <div key={location.location_id} className="card p-6">
                <div className="flex items-start justify-between mb-4">
                  <div className="flex-1">
                    <h3 className="text-lg font-bold text-navy-800">
                      {location.organization?.organization_name || 'Unknown Organization'}
                    </h3>
                    {location.location_name && (
                      <p className="text-sm text-neutral-600 mt-1">{location.location_name}</p>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-2">
                    <span className="px-3 py-1 bg-amber-100 text-amber-900 rounded-full text-xs font-semibold">
                      Needs Review
                    </span>
                    <span className="px-3 py-1 bg-neutral-100 text-neutral-800 rounded-full text-xs font-medium">
                      Source: {sourceLabel(location.source_system, location.achc_source_id)}
                    </span>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4 text-sm">
                  <div>
                    <p className="text-neutral-600">Address:</p>
                    <p className="font-medium text-navy-800">
                      {location.address_line_1}<br />
                      {location.city}, {location.state} {location.postal_code}
                    </p>
                  </div>
                  <div>
                    <p className="text-neutral-600">Contact:</p>
                    <p className="font-medium text-navy-800">
                      {location.public_phone || 'No phone'}
                      {location.public_email && <><br />{location.public_email}</>}
                    </p>
                  </div>
                </div>

                {location.service_types && location.service_types.length > 0 && (
                  <div className="mb-4">
                    <p className="text-xs text-neutral-600 mb-2">Service Types:</p>
                    <div className="flex flex-wrap gap-2">
                      {location.service_types.map((st) => (
                        <span key={st.service_type_id} className="px-2 py-1 bg-primary-100 text-primary-700 rounded text-xs font-medium">
                          {st.service_type_name}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {location.website_url && (
                  <a
                    href={location.website_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-primary-600 hover:text-primary-700 font-medium inline-flex items-center gap-1 mb-4"
                  >
                    Visit Website <ExternalLink className="w-3 h-3" aria-hidden="true" />
                  </a>
                )}

                <div className="mb-4 rounded-lg border border-neutral-300 p-4">
                  <p className="text-sm font-semibold text-navy-800 mb-2">
                    Site-ready checks: {readiness.checks.filter((c) => c.passed).length} of {readiness.checks.length} passed
                  </p>
                  <ul className="space-y-1 text-sm">
                    {readiness.checks.map((check) => (
                      <li key={check.id} className="flex items-start gap-2">
                        {check.passed ? (
                          <CheckCircle2 className="w-4 h-4 mt-0.5 text-success-700 flex-shrink-0" aria-hidden="true" />
                        ) : (
                          <XCircle className="w-4 h-4 mt-0.5 text-red-700 flex-shrink-0" aria-hidden="true" />
                        )}
                        <span>
                          <span className="sr-only">{check.passed ? 'Passed: ' : 'Failed: '}</span>
                          <span className="font-medium text-navy-800">{check.label}</span>
                          {!check.passed && <span className="text-neutral-700"> - {check.detail}</span>}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="flex gap-3 pt-4 border-t border-neutral-500">
                  <button
                    onClick={() => handleApprove(location.location_id)}
                    disabled={!readiness.ready}
                    aria-describedby={readiness.ready ? undefined : `not-ready-${location.location_id}`}
                    className="flex-1 px-4 py-2.5 bg-success-600 text-white rounded-lg hover:bg-success-700 transition-colors font-medium flex items-center justify-center gap-2 disabled:bg-neutral-300 disabled:text-neutral-700 disabled:cursor-not-allowed"
                  >
                    <CheckCircle2 className="w-4 h-4" aria-hidden="true" />
                    Approve & Publish
                  </button>
                  <button
                    onClick={() => handleReject(location.location_id)}
                    className="flex-1 px-4 py-2.5 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors font-medium flex items-center justify-center gap-2"
                  >
                    <XCircle className="w-4 h-4" aria-hidden="true" />
                    Reject
                  </button>
                </div>
                {!readiness.ready && (
                  <p id={`not-ready-${location.location_id}`} className="mt-2 text-sm text-neutral-700">
                    Approve is off until every check passes.
                  </p>
                )}
              </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
