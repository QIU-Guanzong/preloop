import { expect } from '@open-wc/testing';

import {
  PREMIUM_FEATURE_LABELS,
  capabilityForFeature,
  planPageUrl,
  premiumFeatureLabel,
} from './premium-features';

describe('premium features (the 402 upgrade contract)', () => {
  it('maps a refused feature onto the capability a plan actually sells', () => {
    // No plan lists "session_titles". All three of these are sold as one
    // capability, so a plan page looking for the feature name would find no
    // plan at all and offer no upgrade.
    expect(capabilityForFeature('session_titles')).to.equal('ai_optimization');
    expect(capabilityForFeature('session_optimization')).to.equal(
      'ai_optimization'
    );
    expect(capabilityForFeature('replay_verification')).to.equal(
      'ai_optimization'
    );
  });

  it('passes a capability through unchanged', () => {
    expect(capabilityForFeature('rbac')).to.equal('rbac');
    expect(capabilityForFeature('price_overrides')).to.equal('price_overrides');
  });

  it('names a feature the way a reader would', () => {
    expect(premiumFeatureLabel('rbac')).to.equal('role-based access control');
    // An unknown name prints as it arrived rather than as an empty sentence.
    expect(premiumFeatureLabel('something_new')).to.equal('something_new');
  });

  it('uses no em dash in any label (founder ruling)', () => {
    for (const label of Object.values(PREMIUM_FEATURE_LABELS)) {
      expect(label).to.not.contain('—');
    }
  });

  it('carries the capability to the plan page, and nothing when there is none', () => {
    expect(planPageUrl('session_titles')).to.equal(
      '/console/settings/account?feature=ai_optimization'
    );
    expect(planPageUrl('')).to.equal('/console/settings/account');
    expect(planPageUrl(null)).to.equal('/console/settings/account');
    expect(planPageUrl(undefined)).to.equal('/console/settings/account');
  });

  it('escapes what it puts in the query', () => {
    expect(planPageUrl('a b&c')).to.equal(
      '/console/settings/account?feature=a%20b%26c'
    );
  });
});
