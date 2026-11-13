import { useState, useEffect, useRef } from 'react';
import { X, MapPin, CheckCircle2, Search, AlertCircle, Loader } from 'lucide-react';
import { supabase } from '../lib/supabase';
import InfoTooltip from './InfoTooltip';
import { useDebounce } from '../hooks/useDebounce';
import { useGoogleMaps } from '../hooks/useGoogleMaps';
import {
  formatPhoneNumber,
  formatPostalCode,
  validateEmail,
  validatePhoneNumber,
  validatePostalCode,
  validateUrl,
  validateRequired,
  normalizeUrl,
  capitalizeWords,
} from '../lib/formValidation';
import { autocompletePlace, getPlaceDetails, geocodeAddress, findBusinessAtAddress, type PlaceDetails, type PlaceAutocompleteResult } from '../lib/googlePlaces';
import type { ServiceType } from '../types/database';
import { SERVICE_CATEGORIES, getCategoryForService } from '../lib/serviceCategories';

interface ListingFormWizardProps {
  locationId?: string;
  onClose: () => void;
  onSave: () => void;
}

interface FormData {
  organization_id?: string;
  organization_dba_name: string;
  organization_legal_name: string;
  location_name: string;
  address_line_1: string;
  address_line_2: string;
  city: string;
  state: string;
  postal_code: string;
  county: string;
  latitude: string;
  longitude: string;
  public_phone: string;
  public_email: string;
  website_url: string;
  service_area_text: string;
  is_enhanced: boolean;
  insurance_accepted: string;
  service_type_ids: string[];
}

interface ValidationErrors {
  [key: string]: string;
}

interface SimilarOrg {
  organization_id: string;
  organization_name: string;
  similarity: number;
}

const STATES = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
  'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
  'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
  'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
  'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
];

export default function ListingFormWizard({ locationId, onClose, onSave }: ListingFormWizardProps) {
  const { isLoaded: googleMapsLoaded } = useGoogleMaps();
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(false);
  const [geocoding, setGeocoding] = useState(false);
  const [searchingPlaces, setSearchingPlaces] = useState(false);
  const [serviceTypes, setServiceTypes] = useState<ServiceType[]>([]);
  const [errors, setErrors] = useState<ValidationErrors>({});
  const [touched, setTouched] = useState<{ [key: string]: boolean }>({});
  const [autocompleteResults, setAutocompleteResults] = useState<PlaceAutocompleteResult[]>([]);
  const [showAutocomplete, setShowAutocomplete] = useState(false);
  const [placeSearchQuery, setPlaceSearchQuery] = useState('');
  const [similarOrgs, setSimilarOrgs] = useState<SimilarOrg[]>([]);
  const [showOrgSuggestion, setShowOrgSuggestion] = useState(false);
  const [draftLocationId, setDraftLocationId] = useState<string | undefined>(locationId);
  const [manualEntry, setManualEntry] = useState(false);
  const [addressCompleted, setAddressCompleted] = useState(false);
  const autocompleteRef = useRef<HTMLDivElement>(null);

  const [formData, setFormData] = useState<FormData>({
    organization_dba_name: '',
    organization_legal_name: '',
    location_name: '',
    address_line_1: '',
    address_line_2: '',
    city: '',
    state: '',
    postal_code: '',
    county: '',
    latitude: '',
    longitude: '',
    public_phone: '',
    public_email: '',
    website_url: '',
    service_area_text: '',
    is_enhanced: false,
    insurance_accepted: '',
    service_type_ids: [],
  });

  const debouncedSearchQuery = useDebounce(placeSearchQuery, 400);
  const debouncedAddressFields = useDebounce(
    `${formData.address_line_1}|${formData.city}|${formData.state}|${formData.postal_code}`,
    1000
  );

  useEffect(() => {
    loadInitialData();
  }, [locationId]);

  useEffect(() => {
    if (formData.organization_dba_name.length >= 3 && step === 2) {
      checkSimilarOrganizations(formData.organization_dba_name);
    } else {
      setSimilarOrgs([]);
      setShowOrgSuggestion(false);
    }
  }, [formData.organization_dba_name, step]);

  useEffect(() => {
    if (
      step === 1 &&
      manualEntry &&
      formData.address_line_1 &&
      formData.city &&
      formData.state &&
      formData.postal_code &&
      validatePostalCode(formData.postal_code) &&
      !geocoding
    ) {
      geocodeCurrentAddress();
    }
  }, [debouncedAddressFields, step, manualEntry]);

  useEffect(() => {
    if (googleMapsLoaded && debouncedSearchQuery.length >= 3) {
      fetchAutocompleteSuggestions();
    } else {
      setAutocompleteResults([]);
      setShowAutocomplete(false);
    }
  }, [debouncedSearchQuery, googleMapsLoaded]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (autocompleteRef.current && !autocompleteRef.current.contains(event.target as Node)) {
        setShowAutocomplete(false);
      }
    }

    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, []);

  async function loadInitialData() {
    try {
      const servicesResponse = await supabase
        .from('service_types')
        .select('*')
        .order('display_order');

      if (servicesResponse.data) setServiceTypes(servicesResponse.data);

      if (locationId) {
        const { data: location } = await supabase
          .from('locations')
          .select('*, location_service_types(service_type_id), organizations(*)')
          .eq('location_id', locationId)
          .single();

        if (location) {
          const org = Array.isArray(location.organizations) ? location.organizations[0] : location.organizations;
          const serviceTypeIds =
            location.location_service_types?.map((lst: any) => lst.service_type_id) || [];

          setFormData({
            organization_id: location.organization_id,
            organization_dba_name: org?.organization_name || '',
            organization_legal_name: (org as any)?.legal_name || '',
            location_name: location.location_name || '',
            address_line_1: location.address_line_1,
            address_line_2: location.address_line_2 || '',
            city: location.city,
            state: location.state,
            postal_code: location.postal_code,
            county: location.county || '',
            latitude: location.latitude?.toString() || '',
            longitude: location.longitude?.toString() || '',
            public_phone: location.public_phone || '',
            public_email: location.public_email || '',
            website_url: location.website_url || '',
            service_area_text: location.service_area_text || '',
            is_enhanced: (location as any).is_enhanced || false,
            insurance_accepted: ((location as any).insurance_accepted || []).join(', '),
            service_type_ids: serviceTypeIds,
          });
        }
      }
    } catch (error) {
      console.error('Error loading data:', error);
    }
  }

  async function checkSimilarOrganizations(name: string) {
    if (!name || name.length < 3) {
      setSimilarOrgs([]);
      setShowOrgSuggestion(false);
      return;
    }

    try {
      const { data } = await supabase
        .from('organizations')
        .select(`
          organization_id,
          organization_name,
          locations!inner(location_id, listing_status)
        `)
        .ilike('organization_name', `%${name}%`)
        .neq('locations.listing_status', 'draft')
        .limit(3);

      if (data && data.length > 0) {
        const uniqueOrgs = Array.from(
          new Map(data.map(org => [org.organization_id, { organization_id: org.organization_id, organization_name: org.organization_name }])).values()
        );
        setSimilarOrgs(uniqueOrgs.map(org => ({ ...org, similarity: 0.8 })));
        setShowOrgSuggestion(true);
      } else {
        setSimilarOrgs([]);
        setShowOrgSuggestion(false);
      }
    } catch (error) {
      console.error('Error checking similar organizations:', error);
      setSimilarOrgs([]);
      setShowOrgSuggestion(false);
    }
  }

  async function fetchAutocompleteSuggestions() {
    setSearchingPlaces(true);
    try {
      const results = await autocompletePlace(placeSearchQuery);
      setAutocompleteResults(results);
      setShowAutocomplete(results.length > 0);
    } catch (error) {
      console.error('Error fetching autocomplete:', error);
    } finally {
      setSearchingPlaces(false);
    }
  }

  async function selectPlace(placeId: string) {
    setLoading(true);
    try {
      const details = await getPlaceDetails(placeId);
      if (details) {
        setFormData((prev) => ({
          ...prev,
          organization_dba_name: prev.organization_dba_name || details.name,
          address_line_1: details.address_line_1,
          city: details.city,
          state: details.state,
          postal_code: details.postal_code,
          county: details.county || '',
          latitude: details.latitude.toString(),
          longitude: details.longitude.toString(),
          public_phone: details.phone ? formatPhoneNumber(details.phone) : '',
          website_url: details.website || '',
        }));
        setShowAutocomplete(false);
        setAutocompleteResults([]);
        setPlaceSearchQuery('');
        setAddressCompleted(true);
        setStep(2);
      }
    } catch (error) {
      console.error('Error selecting place:', error);
      alert('Failed to import from Google Maps. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  async function geocodeCurrentAddress() {
    const address = `${formData.address_line_1}, ${formData.city}, ${formData.state} ${formData.postal_code}`;

    setGeocoding(true);
    try {
      const [geocodeResult, businessName] = await Promise.all([
        geocodeAddress(address),
        formData.organization_dba_name ? null : findBusinessAtAddress(address)
      ]);

      if (geocodeResult) {
        setFormData((prev) => ({
          ...prev,
          latitude: geocodeResult.lat.toString(),
          longitude: geocodeResult.lng.toString(),
          county: geocodeResult.county || prev.county,
          organization_dba_name: businessName || prev.organization_dba_name,
        }));
      }
    } catch (error) {
      console.error('Geocoding error:', error);
    } finally {
      setGeocoding(false);
    }
  }

  function handleInputChange(field: keyof FormData, value: any) {
    setFormData((prev) => ({ ...prev, [field]: value }));
    setTouched((prev) => ({ ...prev, [field]: true }));
    validateField(field, value);
  }

  function handlePhoneChange(value: string) {
    const formatted = formatPhoneNumber(value);
    handleInputChange('public_phone', formatted);
  }

  function handlePostalCodeChange(value: string) {
    const formatted = formatPostalCode(value);
    handleInputChange('postal_code', formatted);
  }

  function validateField(field: keyof FormData, value: any) {
    const newErrors = { ...errors };

    switch (field) {
      case 'organization_dba_name':
        if (!validateRequired(value)) {
          newErrors[field] = 'DBA name is required';
        } else {
          delete newErrors[field];
        }
        break;

      case 'address_line_1':
      case 'city':
      case 'state':
        if (!validateRequired(value)) {
          newErrors[field] = 'This field is required';
        } else {
          delete newErrors[field];
        }
        break;

      case 'postal_code':
        if (!validateRequired(value)) {
          newErrors[field] = 'This field is required';
        } else if (!validatePostalCode(value)) {
          newErrors[field] = 'Invalid postal code';
        } else {
          delete newErrors[field];
        }
        break;

      case 'public_phone':
        if (value && !validatePhoneNumber(value)) {
          newErrors[field] = 'Phone number must be 10 digits';
        } else {
          delete newErrors[field];
        }
        break;

      case 'public_email':
        if (value && !validateEmail(value)) {
          newErrors[field] = 'Invalid email address';
        } else {
          delete newErrors[field];
        }
        break;

      case 'website_url':
        if (value && !validateUrl(normalizeUrl(value))) {
          newErrors[field] = 'Invalid URL';
        } else {
          delete newErrors[field];
        }
        break;

      default:
        break;
    }

    setErrors(newErrors);
  }

  function validateStep1(): boolean {
    const newErrors: ValidationErrors = {};

    if (!validateRequired(formData.address_line_1)) {
      newErrors.address_line_1 = 'Address is required';
    }

    if (!validateRequired(formData.city)) {
      newErrors.city = 'City is required';
    }

    if (!validateRequired(formData.state)) {
      newErrors.state = 'State is required';
    }

    if (!validateRequired(formData.postal_code) || !validatePostalCode(formData.postal_code)) {
      newErrors.postal_code = 'Valid postal code is required';
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  }

  function validateStep2(): boolean {
    const newErrors: ValidationErrors = {};

    if (!validateRequired(formData.organization_dba_name)) {
      newErrors.organization_dba_name = 'DBA name is required';
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  }

  async function handleStep1Continue() {
    if (!validateStep1()) {
      return;
    }

    setAddressCompleted(true);
    setStep(2);
  }

  async function handleStep2Continue() {
    if (!validateStep2()) {
      return;
    }

    setLoading(true);

    try {
      if (!formData.latitude) {
        await geocodeCurrentAddress();
      }
      setStep(3);
    } catch (error) {
      console.error('Error:', error);
      const errorMessage = error instanceof Error ? error.message : 'Failed to continue. Please try again.';
      alert(errorMessage);
    } finally {
      setLoading(false);
    }
  }

  async function handleFinalSubmit() {
    setLoading(true);

    try {
      let orgId = formData.organization_id;

      if (!orgId) {
        const orgSlug = formData.organization_dba_name
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, '-')
          .replace(/^-|-$/g, '');

        const { data: existingOrg } = await supabase
          .from('organizations')
          .select('organization_id')
          .eq('organization_slug', orgSlug)
          .maybeSingle();

        if (existingOrg) {
          orgId = existingOrg.organization_id;
        } else {
          const orgData: any = {
            organization_name: formData.organization_dba_name,
            organization_slug: orgSlug,
          };

          if (formData.organization_legal_name) {
            orgData.legal_name = formData.organization_legal_name;
          }

          const { data: newOrg, error: orgError } = await supabase
            .from('organizations')
            .insert(orgData)
            .select()
            .single();

          if (orgError) throw orgError;
          orgId = newOrg.organization_id;
        }
      }

      const insuranceArray = formData.insurance_accepted
        ? formData.insurance_accepted.split(',').map((s) => s.trim()).filter(Boolean)
        : [];

      const locationData: any = {
        organization_id: orgId,
        location_name: formData.location_name || null,
        address_line_1: formData.address_line_1,
        address_line_2: formData.address_line_2 || null,
        city: capitalizeWords(formData.city),
        state: formData.state.toUpperCase(),
        postal_code: formData.postal_code,
        county: formData.county || null,
        latitude: formData.latitude ? parseFloat(formData.latitude) : null,
        longitude: formData.longitude ? parseFloat(formData.longitude) : null,
        public_phone: formData.public_phone || null,
        public_email: formData.public_email || null,
        website_url: formData.website_url ? normalizeUrl(formData.website_url) : null,
        service_area_text: formData.service_area_text || null,
        is_enhanced: formData.is_enhanced,
        insurance_accepted: insuranceArray.length > 0 ? insuranceArray : null,
        featured_in_alphabetical: formData.is_enhanced,
        listing_status: 'needs_review',
        // The column defaults to 'achc'; tag hand-entered listings so the
        // Review Queue can tell them apart (see src/lib/siteReadiness.ts).
        source_system: 'admin_manual',
      };

      const { data: newLocation, error: locationError } = await supabase
        .from('locations')
        .insert(locationData)
        .select()
        .single();

      if (locationError) throw locationError;

      const locationId = newLocation.location_id;

      if (formData.service_type_ids.length > 0) {
        const serviceTypeRecords = formData.service_type_ids.map((serviceTypeId) => ({
          location_id: locationId,
          service_type_id: serviceTypeId,
        }));

        const { error: serviceError } = await supabase
          .from('location_service_types')
          .insert(serviceTypeRecords);

        if (serviceError) throw serviceError;

        const { data: selectedServiceTypes } = await supabase
          .from('service_types')
          .select('service_type_slug')
          .in('service_type_id', formData.service_type_ids);

        if (selectedServiceTypes) {
          const categoryTags = new Set<string>();
          selectedServiceTypes.forEach(st => {
            const categoryTag = getCategoryForService(st.service_type_slug);
            if (categoryTag) {
              categoryTags.add(categoryTag);
            }
          });

          if (categoryTags.size > 0) {
            const { data: existingTags } = await supabase
              .from('search_tags')
              .select('tag_id, tag_name')
              .in('tag_name', Array.from(categoryTags));

            const existingTagMap = new Map(
              existingTags?.map(tag => [tag.tag_name, tag.tag_id]) || []
            );

            const tagRecords = Array.from(categoryTags).map(tagName => ({
              location_id: locationId,
              tag_id: existingTagMap.get(tagName),
              tag_name: tagName,
            }));

            await supabase
              .from('location_tags')
              .insert(tagRecords);
          }
        }
      }

      onSave();
      onClose();
    } catch (error) {
      console.error('Error finalizing listing:', error);
      alert('Failed to submit listing. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  function selectSimilarOrg(orgId: string, orgName: string) {
    setFormData((prev) => ({
      ...prev,
      organization_id: orgId,
      organization_dba_name: orgName,
    }));
    setShowOrgSuggestion(false);
    setSimilarOrgs([]);
  }

  function dismissSuggestion() {
    setShowOrgSuggestion(false);
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4 overflow-y-auto">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl my-8 max-h-[90vh] overflow-y-auto">
        <div className="sticky top-0 bg-white border-b border-neutral-500 z-10">
          <div className="flex items-center justify-between p-6">
            <div>
              <h2 className="text-2xl font-bold text-navy-800">
                {locationId ? 'Edit Listing' : 'Add New Listing'}
              </h2>
              <div className="flex items-center gap-2 mt-2">
                <div className={`flex items-center gap-2 ${step >= 1 ? 'text-primary-600' : 'text-neutral-400'}`}>
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center font-semibold ${step >= 1 ? 'bg-primary-100' : 'bg-neutral-100'}`}>
                    1
                  </div>
                  <span className="text-sm font-medium">Quick Add</span>
                </div>
                <div className="w-12 h-0.5 bg-neutral-500" />
                <div className={`flex items-center gap-2 ${step >= 2 ? 'text-primary-600' : 'text-neutral-400'}`}>
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center font-semibold ${step >= 2 ? 'bg-primary-100' : 'bg-neutral-100'}`}>
                    2
                  </div>
                  <span className="text-sm font-medium">Basic Info</span>
                </div>
                <div className="w-12 h-0.5 bg-neutral-500" />
                <div className={`flex items-center gap-2 ${step >= 3 ? 'text-primary-600' : 'text-neutral-400'}`}>
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center font-semibold ${step >= 3 ? 'bg-primary-100' : 'bg-neutral-100'}`}>
                    3
                  </div>
                  <span className="text-sm font-medium">Details</span>
                </div>
              </div>
            </div>
            <button
              onClick={onClose}
              className="text-neutral-500 hover:text-neutral-600 transition-colors"
              aria-label="Close"
            >
              <X className="w-6 h-6" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="p-6">
          {step === 1 && !manualEntry && (
            <div className="space-y-6">
              <div className="text-center mb-8">
                <h3 className="text-xl font-bold text-navy-800 mb-2">Find Your Business</h3>
                <p className="text-neutral-600">Search for your business on Google Maps to auto-fill details</p>
              </div>

              <div className="bg-blue-50 border border-blue-200 rounded-lg p-6">
                <div className="flex items-start gap-3">
                  <Search className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" aria-hidden="true" />
                  <div className="flex-1">
                    <p className="font-semibold text-blue-900 mb-2">Quick Import from Google Maps</p>
                    <p className="text-sm text-blue-800 mb-3">
                      Start typing a business name and location to see suggestions
                    </p>
                    {!googleMapsLoaded && (
                      <p className="text-xs text-amber-600 mb-2 flex items-center gap-1">
                        <Loader className="w-3 h-3 animate-spin" aria-hidden="true" />
                        Loading Google Maps...
                      </p>
                    )}
                    <div className="relative" ref={autocompleteRef}>
                      <input
                        type="text"
                        value={placeSearchQuery}
                        onChange={(e) => {
                          setPlaceSearchQuery(e.target.value);
                          if (e.target.value.length >= 3) {
                            setShowAutocomplete(true);
                          }
                        }}
                        onFocus={() => {
                          if (autocompleteResults.length > 0) {
                            setShowAutocomplete(true);
                          }
                        }}
                        disabled={!googleMapsLoaded}
                        placeholder={googleMapsLoaded ? "e.g., ABC Home Care Dallas TX" : "Loading Google Maps..."}
                        className="w-full px-4 py-3 border border-blue-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-neutral-100 disabled:cursor-not-allowed"
                      />
                      {searchingPlaces && (
                        <div className="absolute right-3 top-1/2 -translate-y-1/2" role="status">
                          <Loader className="w-4 h-4 text-blue-600 animate-spin" aria-hidden="true" />
                          <span className="sr-only">Searching…</span>
                        </div>
                      )}

                      {showAutocomplete && autocompleteResults.length > 0 && (
                        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-neutral-500 rounded-lg shadow-lg z-50 max-h-80 overflow-y-auto">
                          {autocompleteResults.map((result) => (
                            <button
                              key={result.place_id}
                              type="button"
                              onClick={() => selectPlace(result.place_id)}
                              className="w-full text-left p-3 hover:bg-blue-50 transition-colors border-b border-neutral-500 last:border-b-0 flex items-start gap-3"
                            >
                              <MapPin className="w-4 h-4 text-neutral-500 flex-shrink-0 mt-1" aria-hidden="true" />
                              <span className="text-neutral-800">{result.description}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex justify-center pt-4">
                <button
                  type="button"
                  onClick={() => setManualEntry(true)}
                  className="text-sm text-neutral-600 hover:text-neutral-800 underline transition-colors"
                >
                  Or enter manually
                </button>
              </div>

              <div className="flex justify-between pt-6 border-t border-neutral-500">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-6 py-2.5 border border-neutral-500 text-neutral-700 rounded-lg hover:bg-neutral-50 transition-colors font-medium"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {step === 1 && manualEntry && (
            <div className="space-y-6">
              <div className="mb-4">
                <button
                  type="button"
                  onClick={() => setManualEntry(false)}
                  className="text-sm text-neutral-600 hover:text-neutral-800 flex items-center gap-1 transition-colors"
                >
                  ← Back to search
                </button>
              </div>

              <div className="text-center mb-6">
                <h3 className="text-xl font-bold text-navy-800 mb-2">Enter Address</h3>
                <p className="text-neutral-600">Provide the location address details</p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-semibold text-navy-800 mb-2">
                    Address Line 1 *
                  </label>
                  <input
                    type="text"
                    value={formData.address_line_1}
                    onChange={(e) => handleInputChange('address_line_1', e.target.value)}
                    placeholder="123 Main Street"
                    className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                      errors.address_line_1 && touched.address_line_1
                        ? 'border-red-500'
                        : 'border-neutral-500'
                    }`}
                  />
                  {errors.address_line_1 && touched.address_line_1 && (
                    <p className="text-sm text-red-600 mt-1">{errors.address_line_1}</p>
                  )}
                </div>

                <div>
                  <label className="block text-sm font-semibold text-navy-800 mb-2">
                    Address Line 2
                  </label>
                  <input
                    type="text"
                    value={formData.address_line_2}
                    onChange={(e) => handleInputChange('address_line_2', e.target.value)}
                    placeholder="Suite 100"
                    className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                  />
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-semibold text-navy-800 mb-2">City *</label>
                    <input
                      type="text"
                      value={formData.city}
                      onChange={(e) => handleInputChange('city', e.target.value)}
                      placeholder="Dallas"
                      className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                        errors.city && touched.city ? 'border-red-500' : 'border-neutral-500'
                      }`}
                    />
                    {errors.city && touched.city && (
                      <p className="text-sm text-red-600 mt-1">{errors.city}</p>
                    )}
                  </div>

                  <div>
                    <label className="block text-sm font-semibold text-navy-800 mb-2">State *</label>
                    <select
                      value={formData.state}
                      onChange={(e) => handleInputChange('state', e.target.value)}
                      className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                        errors.state && touched.state ? 'border-red-500' : 'border-neutral-500'
                      }`}
                    >
                      <option value="">Select State</option>
                      {STATES.map((state) => (
                        <option key={state} value={state}>
                          {state}
                        </option>
                      ))}
                    </select>
                    {errors.state && touched.state && (
                      <p className="text-sm text-red-600 mt-1">{errors.state}</p>
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-semibold text-navy-800 mb-2">
                      Postal Code *
                    </label>
                    <input
                      type="text"
                      value={formData.postal_code}
                      onChange={(e) => handlePostalCodeChange(e.target.value)}
                      placeholder="75201"
                      maxLength={10}
                      className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                        errors.postal_code && touched.postal_code
                          ? 'border-red-500'
                          : 'border-neutral-500'
                      }`}
                    />
                    {errors.postal_code && touched.postal_code && (
                      <p className="text-sm text-red-600 mt-1">{errors.postal_code}</p>
                    )}
                  </div>

                  <div>
                    <label className="block text-sm font-semibold text-navy-800 mb-2">County</label>
                    <input
                      type="text"
                      value={formData.county}
                      readOnly
                      placeholder="Auto-populated"
                      className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg bg-neutral-50 text-neutral-600 cursor-not-allowed"
                    />
                  </div>
                </div>
              </div>

              <div className="flex justify-between pt-6 border-t border-neutral-500">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-6 py-2.5 border border-neutral-500 text-neutral-700 rounded-lg hover:bg-neutral-50 transition-colors font-medium"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleStep1Continue}
                  disabled={loading}
                  className="px-6 py-2.5 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Continue
                </button>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-6">
              <div className="text-center mb-6">
                <h3 className="text-xl font-bold text-navy-800 mb-2">Basic Information</h3>
                <p className="text-neutral-600">Confirm address and provide business details</p>
              </div>

              <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                <h4 className="text-sm font-semibold text-blue-900 mb-3 flex items-center gap-2">
                  <MapPin className="w-4 h-4" aria-hidden="true" />
                  Address
                </h4>
                <div className="text-sm text-blue-800 space-y-1">
                  <p className="font-medium">{formData.address_line_1}</p>
                  {formData.address_line_2 && <p>{formData.address_line_2}</p>}
                  <p>{formData.city}, {formData.state} {formData.postal_code}</p>
                  {formData.county && <p className="text-xs text-blue-700">County: {formData.county}</p>}
                </div>
              </div>

              <div>
                <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                  Organization DBA Name *
                  <InfoTooltip content="The 'Doing Business As' name - this is the name customers know the business by" />
                </label>
                <input
                  type="text"
                  value={formData.organization_dba_name}
                  onChange={(e) => handleInputChange('organization_dba_name', e.target.value)}
                  placeholder="e.g., ABC Home Care"
                  className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                    errors.organization_dba_name && touched.organization_dba_name
                      ? 'border-red-500'
                      : 'border-neutral-500'
                  }`}
                />
                {errors.organization_dba_name && touched.organization_dba_name && (
                  <p className="text-sm text-red-600 mt-1">{errors.organization_dba_name}</p>
                )}

                {showOrgSuggestion && similarOrgs.length > 0 && (
                  <div className="mt-3 bg-amber-50 border border-amber-200 rounded-lg p-4">
                    <p className="text-sm font-semibold text-amber-900 mb-2">
                      Similar organizations found. Did you mean:
                    </p>
                    <div className="space-y-2">
                      {similarOrgs.map((org) => (
                        <div key={org.organization_id} className="flex items-center justify-between">
                          <span className="text-sm text-neutral-700">{org.organization_name}</span>
                          <div className="flex gap-2">
                            <button
                              type="button"
                              onClick={() => selectSimilarOrg(org.organization_id, org.organization_name)}
                              className="text-xs bg-primary-600 hover:bg-primary-700 text-white px-3 py-1 rounded-md transition-colors font-medium"
                            >
                              Yes
                            </button>
                            <button
                              type="button"
                              onClick={dismissSuggestion}
                              className="text-xs bg-neutral-200 hover:bg-neutral-300 text-neutral-700 px-3 py-1 rounded-md transition-colors font-medium"
                            >
                              No
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div>
                <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                  Legal Business Name
                  <InfoTooltip content="The official registered business name (optional but recommended for verification)" />
                </label>
                <input
                  type="text"
                  value={formData.organization_legal_name}
                  onChange={(e) => handleInputChange('organization_legal_name', e.target.value)}
                  placeholder="e.g., ABC Home Care Services, LLC"
                  className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                />
                <p className="text-xs text-neutral-500 mt-1">
                  Recommended for accurate business records
                </p>
              </div>

              <div>
                <label className="block text-sm font-semibold text-navy-800 mb-2">
                  Location Name (Optional)
                </label>
                <input
                  type="text"
                  value={formData.location_name}
                  onChange={(e) => handleInputChange('location_name', e.target.value)}
                  placeholder="e.g., North Dallas Office"
                  className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                />
                <p className="text-xs text-neutral-500 mt-1">
                  Leave blank if organization has only one location
                </p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                    Phone Number
                    <InfoTooltip content="Public-facing phone number for customers to contact this location" />
                  </label>
                  <input
                    type="tel"
                    value={formData.public_phone}
                    onChange={(e) => handlePhoneChange(e.target.value)}
                    placeholder="(555) 123-4567"
                    maxLength={14}
                    className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                      errors.public_phone && touched.public_phone
                        ? 'border-red-500'
                        : 'border-neutral-500'
                    }`}
                  />
                  {errors.public_phone && touched.public_phone && (
                    <p className="text-sm text-red-600 mt-1">{errors.public_phone}</p>
                  )}
                </div>

                <div>
                  <label className="block text-sm font-semibold text-navy-800 mb-2">Email</label>
                  <input
                    type="email"
                    value={formData.public_email}
                    onChange={(e) => handleInputChange('public_email', e.target.value)}
                    placeholder="contact@example.com"
                    className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                      errors.public_email && touched.public_email
                        ? 'border-red-500'
                        : 'border-neutral-500'
                    }`}
                  />
                  {errors.public_email && touched.public_email && (
                    <p className="text-sm text-red-600 mt-1">{errors.public_email}</p>
                  )}
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-navy-800 mb-2">Website</label>
                <input
                  type="text"
                  value={formData.website_url}
                  onChange={(e) => handleInputChange('website_url', e.target.value)}
                  placeholder="www.example.com"
                  className={`w-full px-4 py-2.5 border rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all ${
                    errors.website_url && touched.website_url ? 'border-red-500' : 'border-neutral-500'
                  }`}
                />
                {errors.website_url && touched.website_url && (
                  <p className="text-sm text-red-600 mt-1">{errors.website_url}</p>
                )}
              </div>

              <div className="flex justify-between pt-6 border-t border-neutral-500">
                <button
                  type="button"
                  onClick={() => {
                    setStep(1);
                    if (!addressCompleted) {
                      setManualEntry(false);
                    }
                  }}
                  className="px-6 py-2.5 border border-neutral-500 text-neutral-700 rounded-lg hover:bg-neutral-50 transition-colors font-medium"
                >
                  Back
                </button>
                <button
                  type="button"
                  onClick={handleStep2Continue}
                  disabled={loading}
                  className="px-6 py-2.5 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loading ? 'Saving...' : 'Continue'}
                </button>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-6">
              <div className="text-center mb-6">
                <h3 className="text-xl font-bold text-navy-800 mb-2">Service Details</h3>
                <p className="text-neutral-600">Final step - add service types and optional enhancements</p>
              </div>

              <div>
                <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                  Service Types *
                  <InfoTooltip content="Select all types of care services this location provides" />
                </label>
                <p className="text-xs text-neutral-500 mb-3">
                  Choose the most specific options that match the services provided
                </p>

                <div className="space-y-3">
                  {Object.entries(SERVICE_CATEGORIES).map(([categoryKey, category]) => {
                    const categoryServices = serviceTypes.filter(st =>
                      category.services.includes(st.service_type_slug)
                    );

                    if (categoryServices.length === 0) return null;

                    return (
                      <div key={categoryKey} className="border border-neutral-500 rounded-lg overflow-hidden">
                        <div className="bg-neutral-100 px-3 py-2 border-b border-neutral-500">
                          <h4 className="text-sm font-bold text-navy-800">{category.label}</h4>
                        </div>
                        <div className="p-3 grid grid-cols-1 md:grid-cols-2 gap-2">
                          {categoryServices.map((serviceType) => (
                            <label
                              key={serviceType.service_type_id}
                              className="flex items-start gap-2 p-2 rounded cursor-pointer hover:bg-neutral-50 transition-colors"
                            >
                              <input
                                type="checkbox"
                                checked={formData.service_type_ids.includes(serviceType.service_type_id)}
                                onChange={(e) => {
                                  const newIds = e.target.checked
                                    ? [...formData.service_type_ids, serviceType.service_type_id]
                                    : formData.service_type_ids.filter((id) => id !== serviceType.service_type_id);
                                  handleInputChange('service_type_ids', newIds);
                                }}
                                className="w-4 h-4 mt-0.5 text-primary-600 rounded focus:ring-2 focus:ring-primary-500"
                              />
                              <div className="flex-1 min-w-0">
                                <span className="text-sm font-medium text-navy-800 block leading-snug">
                                  {serviceType.service_type_name}
                                </span>
                                {serviceType.description && (
                                  <span className="text-xs text-neutral-500 leading-snug">{serviceType.description}</span>
                                )}
                              </div>
                            </label>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="border-t border-neutral-500 pt-6">
                <h4 className="text-base font-bold text-navy-800 mb-4">Optional Enhancements</h4>

                <div className="flex items-center gap-3 mb-4">
                  <input
                    type="checkbox"
                    id="is_enhanced_step3"
                    checked={formData.is_enhanced}
                    onChange={(e) => handleInputChange('is_enhanced', e.target.checked)}
                    className="w-5 h-5 text-primary-600 rounded focus:ring-2 focus:ring-primary-500"
                  />
                  <label htmlFor="is_enhanced_step3" className="flex items-center text-sm font-semibold text-navy-800">
                    Enhanced Listing (Premium)
                    <InfoTooltip content="Enhanced listings appear with a sponsored badge and show additional details like insurance accepted and service area" />
                  </label>
                </div>

                {formData.is_enhanced && (
                  <div className="space-y-4 bg-amber-50 border border-amber-200 rounded-lg p-4">
                    <p className="text-sm font-medium text-amber-900">
                      Enhanced Listing Features
                    </p>

                    <div>
                      <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                        Insurance Accepted
                        <InfoTooltip content="List all insurance providers accepted. Will be scraped from website if available." />
                      </label>
                      <input
                        type="text"
                        value={formData.insurance_accepted}
                        onChange={(e) => handleInputChange('insurance_accepted', e.target.value)}
                        placeholder="Medicare, Medicaid, Blue Cross, Aetna"
                        className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                      />
                      <p className="text-xs text-neutral-500 mt-1">Separate multiple with commas</p>
                    </div>

                    <div>
                      <label className="flex items-center text-sm font-semibold text-navy-800 mb-2">
                        Service Area Description
                        <InfoTooltip content="Describe the geographic area served by this location" />
                      </label>
                      <textarea
                        value={formData.service_area_text}
                        onChange={(e) => handleInputChange('service_area_text', e.target.value)}
                        placeholder="e.g., Serving Harris County and surrounding areas"
                        rows={3}
                        className="w-full px-4 py-2.5 border border-neutral-500 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all resize-none"
                      />
                    </div>
                  </div>
                )}
              </div>

              <div className="flex justify-between pt-6 border-t border-neutral-500">
                <button
                  type="button"
                  onClick={() => setStep(2)}
                  className="px-6 py-2.5 border border-neutral-500 text-neutral-700 rounded-lg hover:bg-neutral-50 transition-colors font-medium"
                >
                  Back
                </button>
                <button
                  type="button"
                  onClick={handleFinalSubmit}
                  disabled={loading || geocoding}
                  className="px-6 py-2.5 bg-success-600 text-white rounded-lg hover:bg-success-700 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                >
                  {loading ? (
                    'Submitting...'
                  ) : (
                    <>
                      <CheckCircle2 className="w-4 h-4" aria-hidden="true" />
                      Submit for Review
                    </>
                  )}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
