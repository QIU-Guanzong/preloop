import { expect } from '@open-wc/testing';

import {
  PREMIUM_FEATURE_LABELS,
  capabilityForFeature,
  cheapestPlanUnlocking,
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

  it('carries the refused feature to the plan page, not the capability', () => {
    // The page maps it to a capability itself, so sending the capability
    // here would buy nothing and cost the only thing the name carries: the
    // words the reader was actually refused in.
    expect(planPageUrl('session_titles')).to.equal(
      '/console/settings/plan?feature=session_titles'
    );
    expect(premiumFeatureLabel('session_titles')).to.equal('AI session titles');
    expect(planPageUrl('')).to.equal('/console/settings/plan');
    expect(planPageUrl(null)).to.equal('/console/settings/plan');
    expect(planPageUrl(undefined)).to.equal('/console/settings/plan');
  });

  it('escapes what it puts in the query', () => {
    expect(planPageUrl('a b&c')).to.equal(
      '/console/settings/plan?feature=a%20b%26c'
    );
  });

  describe('the cheapest plan that unlocks a feature', () => {
    const plans = [
      { id: 'free', price_monthly: 0, capabilities: [] },
      { id: 'pro', price_monthly: 12, capabilities: ['ai_optimization'] },
      {
        id: 'team',
        price_monthly: 120,
        capabilities: ['ai_optimization', 'rbac'],
      },
      {
        id: 'teams',
        price_monthly: 29,
        is_legacy: true,
        capabilities: ['ai_optimization', 'rbac'],
      },
      {
        id: 'enterprise',
        price_monthly: null,
        purchasable: false,
        capabilities: ['ai_optimization', 'rbac'],
      },
    ];

    it('maps the refused feature to a capability before matching', () => {
      expect(cheapestPlanUnlocking(plans, 'session_titles')).to.equal('pro');
    });

    it('skips a plan that does not publish the capability', () => {
      expect(cheapestPlanUnlocking(plans, 'rbac')).to.equal('team');
    });

    it('skips legacy, quote-only and unpriced plans', () => {
      // Legacy Teams is cheaper than Team and holds rbac, but an account
      // cannot newly take it, and Enterprise has no price to compare.
      expect(cheapestPlanUnlocking(plans, 'rbac')).to.not.equal('teams');
      expect(cheapestPlanUnlocking(plans, 'rbac')).to.not.equal('enterprise');
    });

    it('offers only a plan the account may actually hold', () => {
      // Pro is cheapest, but the server blocked it, so the answer is the
      // next plan that unlocks the same thing rather than a card whose
      // button cannot be pressed.
      expect(
        cheapestPlanUnlocking(plans, 'session_titles', (p) => p.id !== 'pro')
      ).to.equal('team');
    });

    it('answers nothing when no plan unlocks it', () => {
      expect(cheapestPlanUnlocking(plans, 'something_new')).to.equal('');
      expect(cheapestPlanUnlocking([], 'rbac')).to.equal('');
    });
  });
});
