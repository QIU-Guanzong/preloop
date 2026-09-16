import type { Comparison, PricingPlan } from '../components/pricing-plans';

/**
 * The brand's pricing content, as published for the public page.
 *
 * The console plan page reads the same file for the same reason the public
 * page does: the cards, the taglines and the comparison rows are marketing
 * copy that an operator edits in one place. Reading it twice from one loader
 * is what keeps a signed-in reader and a visitor looking at the same plans.
 */
export interface PricingContent {
  title: string;
  lead: string;
  cloudLabel?: string;
  cloudLead?: string;
  dedicatedLabel?: string;
  dedicatedLead?: string;
  billingToggle?: boolean;
  plans: PricingPlan[];
  comparison: Comparison | null;
  dedicatedPlans: PricingPlan[];
  dedicatedComparison: Comparison | null;
  faqs: { q: string; a: string }[];
}

/** One raw plan entry from the content file. */
function toPlan(p: any, fallback?: 'cloud' | 'dedicated'): PricingPlan {
  return {
    id: p.id,
    name: p.name,
    subtitle: p.subtitle,
    price_monthly: p.price_monthly ?? null,
    price_annually: p.price_annually ?? null,
    price_label: p.price_label,
    price_note: p.price_note,
    price_note_annual: p.price_note_annual,
    tagline: p.tagline,
    badge: p.badge,
    highlight: p.highlight,
    cta_text: p.cta_text,
    cta_url: p.cta_url,
    deployment:
      (p.deployment ?? fallback) === 'dedicated' ? 'dedicated' : 'cloud',
    description: p.description,
    features: p.features || [],
  };
}

/**
 * Read the published pricing content.
 *
 * Throws when the file is missing or unreadable: a page that cannot say what
 * a plan costs must say so rather than draw an empty ladder.
 */
export async function loadPricingContent(): Promise<PricingContent> {
  const response = await fetch('/landing-content.json');
  if (!response.ok) {
    throw new Error(`Failed to load pricing content: ${response.statusText}`);
  }
  const content = await response.json();
  const pricing = content.pricing || {};
  return {
    title: pricing.title || 'Pricing',
    lead: pricing.lead || '',
    cloudLabel: pricing.cloud_label,
    cloudLead: pricing.cloud_lead,
    dedicatedLabel: pricing.dedicated?.label,
    dedicatedLead: pricing.dedicated?.lead,
    billingToggle:
      typeof pricing.billing_toggle === 'boolean'
        ? pricing.billing_toggle
        : undefined,
    plans: Array.isArray(pricing.plans)
      ? pricing.plans.map((p: any) => toPlan(p))
      : [],
    comparison:
      pricing.comparison && Array.isArray(pricing.comparison.groups)
        ? (pricing.comparison as Comparison)
        : null,
    dedicatedPlans: Array.isArray(pricing.dedicated?.plans)
      ? pricing.dedicated.plans.map((p: any) => toPlan(p, 'dedicated'))
      : [],
    dedicatedComparison: Array.isArray(pricing.dedicated?.comparison?.groups)
      ? (pricing.dedicated.comparison as Comparison)
      : null,
    faqs: Array.isArray(pricing.faqs) ? pricing.faqs : [],
  };
}

/** Hosted subscriptions only: the console sells nothing self-hosted. */
export function cloudPlans(content: PricingContent): PricingPlan[] {
  return content.plans.filter((p) => p.deployment !== 'dedicated');
}
