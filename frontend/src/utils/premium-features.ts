/**
 * The 402 upgrade contract, as the console reads it.
 *
 * A gated endpoint answers `{"detail": {"code": "upgrade_required",
 * "feature": "<name>"}}`. Two kinds of name arrive there: a plan capability
 * (`ai_optimization`, `price_overrides`, `rbac`), which is what a plan
 * actually sells, and the caller-facing name of the thing that was blocked
 * (`session_optimization`, `replay_verification`, `session_titles`), which is
 * what the reader recognises. This module keeps the label a reader sees and
 * the capability a plan has to include in one place, so the dialog, the plan
 * picker and the plan page cannot disagree about what an upgrade buys.
 */

/** Human-readable names for gated features (402 upgrade contract). */
export const PREMIUM_FEATURE_LABELS: Record<string, string> = {
  session_optimization: 'AI session optimization',
  replay_verification: 'replay verification',
  session_titles: 'AI session titles',
  ai_optimization: 'built-in model optimization',
  value_reviews: 'value reviews',
  rbac: 'role-based access control',
  team_approvals: 'team approval workflows',
  price_overrides: 'model price overrides',
  reconciliation: 'provider billing reconciliation',
};

/**
 * Feature names that are not themselves plan capabilities.
 *
 * Everything that runs analysis on a built-in hosted model is sold as one
 * capability, so all three map onto it rather than onto a capability no plan
 * lists (a plan page looking for `session_optimization` in a plan's
 * capability array would find it nowhere and offer no upgrade at all).
 */
const FEATURE_CAPABILITIES: Record<string, string> = {
  session_optimization: 'ai_optimization',
  replay_verification: 'ai_optimization',
  session_titles: 'ai_optimization',
};

/** The plan capability that unlocks `feature`, or the feature itself. */
export function capabilityForFeature(feature: string): string {
  return FEATURE_CAPABILITIES[feature] ?? feature;
}

/** What to call `feature` in a sentence. Unknown names print as they arrive. */
export function premiumFeatureLabel(feature: string): string {
  return PREMIUM_FEATURE_LABELS[feature] || feature;
}

/**
 * Where "see the plans" goes.
 *
 * One constant because three surfaces (the upgrade dialog, the account page's
 * "Choose a plan", any future nudge) have to agree on it: moving the page
 * means editing this line, not hunting for string literals.
 */
export const PLAN_PAGE_PATH = '/console/settings/plan';

/**
 * The plan page URL for a gated feature.
 *
 * The feature travels in the query so the page can preselect the cheapest
 * plan that actually includes it. The dialog used to buy a hardcoded plan
 * instead, which was wrong in both directions: too much plan for a feature
 * the entry plan includes, and a refused checkout for a feature it does not.
 */
export function planPageUrl(feature?: string | null): string {
  const capability = feature ? capabilityForFeature(feature) : '';
  return capability
    ? `${PLAN_PAGE_PATH}?feature=${encodeURIComponent(capability)}`
    : PLAN_PAGE_PATH;
}
