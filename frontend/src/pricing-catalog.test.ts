import { expect } from '@open-wc/testing';
import { applyPricingCatalog } from './pricing-catalog';
import type { PricingConfig } from './brand-config';

const config: PricingConfig = {
  plans: [
    {
      id: 'free',
      name: 'Free',
      price_monthly: 99,
      price_annually: 990,
      features: [],
    },
    {
      id: 'pro',
      name: 'Pro',
      price_monthly: 99,
      price_annually: 990,
      features: [],
    },
    {
      id: 'team',
      name: 'Team',
      price_monthly: 99,
      price_annually: 990,
      features: [],
    },
    {
      id: 'enterprise',
      name: 'Enterprise',
      price_monthly: null,
      price_annually: null,
      price_label: 'from $30k/yr',
      features: [],
    },
  ],
};
const catalog = () => ({
  plans: [
    ['free', 0, 183],
    ['pro', 10, 365],
    ['team', 90, 730],
    ['enterprise', null, -1],
  ].map(([id, monthly, days]) => ({
    id: String(id),
    name: String(id),
    price_monthly: monthly as number | null,
    price_annually: monthly === null ? null : Number(monthly) * 10,
    purchasable: id !== 'enterprise',
    legacy: false,
    capabilities:
      id === 'free'
        ? []
        : id === 'pro'
          ? ['ai_optimization']
          : ['ai_optimization', 'rbac'],
    features: {
      max_users: id === 'enterprise' ? 100 : id === 'team' ? 5 : 1,
      max_agents: id === 'free' ? 3 : -1,
      byok_ingest_tokens_monthly:
        id === 'enterprise' ? -1 : id === 'free' ? 10000000 : 100000000,
      retention_days: Number(days),
      hosted_models_monthly_limit_usd:
        id === 'free' || id === 'enterprise' ? null : 2,
      ...(id === 'free' ? { hosted_credit_one_time_usd: 0.5 } : {}),
    },
  })),
  storage_retention: { minimum_days: 183, legal_holds_override: true },
});

describe('Public pricing from billing catalog', () => {
  it('replaces stale brand prices and history with exact catalog values', () => {
    const result = applyPricingCatalog(config, catalog());
    expect(result.plans[1].price_monthly).to.equal(10);
    expect(result.plans[1].price_annually).to.equal(100);
    const row = result.comparison!.groups[0].rows.find(
      (r) => r.label === 'Analytics history'
    )!;
    expect(row.values).to.deep.equal({
      free: '6 months (183 days)',
      pro: '1 year',
      team: '2 years',
      enterprise: 'Custom',
    });
    expect(result.comparison!.note)
      .to.include('183 days')
      .and.include('Older analytics are periodically removed')
      .and.include('longer grandfathered commitments remain protected');
  });
  it('does not advertise planned capabilities absent from the shipped catalog', () => {
    const result = applyPricingCatalog(config, catalog());
    const rows = result.comparison!.groups.flatMap((g) => g.rows);
    expect(rows.map((r) => r.label)).not.to.include('Value reviews');
    expect(rows.map((r) => r.label)).not.to.include('SSO and SAML');
    expect(
      rows.find((r) => r.label === 'Role-based access control')!.values.pro
    ).to.equal(false);
  });
  it('never converts the free lifetime credit to a recurring allowance', () => {
    const result = applyPricingCatalog(config, catalog());
    const row = result.comparison!.groups[0].rows.find(
      (r) => r.label === 'Built-in model allowance'
    )!;
    expect(row.values.free).to.equal('$0.50 one-time credit');
  });
  it('hides a configured legacy entry and preserves Enterprise contact-only', () => {
    const source = catalog();
    source.plans.push({
      ...source.plans[1],
      id: 'teams',
      name: 'Legacy Teams',
      legacy: true,
    });
    const result = applyPricingCatalog(
      {
        ...config,
        plans: [...config.plans, { ...config.plans[1], id: 'teams' }],
      },
      source
    );
    expect(result.plans.some((p) => p.id === 'teams')).to.equal(false);
    expect(result.plans.find((p) => p.id === 'enterprise')!.cta_url).to.equal(
      '/request-demo'
    );
  });
  it('fails the build input instead of silently publishing a missing plan or entitlement', () => {
    const source = catalog();
    source.plans = source.plans.filter((p) => p.id !== 'pro');
    expect(() => applyPricingCatalog(config, source)).to.throw(
      'missing from the billing catalog'
    );
    const invalid = catalog();
    (invalid.plans[1] as any).capabilities = undefined;
    expect(() => applyPricingCatalog(config, invalid)).to.throw(
      'verified limits/capabilities'
    );
  });
});
