import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './pricing-view';
import { PublicPricingView } from './pricing-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const CONTENT = {
  pricing: {
    title: 'Simple Pricing',
    lead: 'Pick a plan that fits.',
    billing_toggle: true,
    plans: [
      {
        id: 'teams',
        name: 'Teams',
        price_monthly: 20,
        price_annually: 200,
        features: ['Feature A', 'Feature B'],
        badge: 'Popular',
      },
      {
        id: 'enterprise',
        name: 'Enterprise',
        price_monthly: null,
        price_annually: null,
        features: ['Everything'],
      },
    ],
    faqs: [{ q: 'Is there a trial?', a: 'Yes, 14 days.' }],
  },
};

/** The 2026 ladder: five bracket-priced plans plus a comparison table. */
const LADDER_CONTENT = {
  pricing: {
    title: 'Pricing',
    lead: 'Start free with your own keys.',
    billing_toggle: true,
    plans: [
      {
        id: 'free',
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
        tagline: 'Try Preloop with your own keys.',
        features: [],
      },
      {
        id: 'pro',
        name: 'Pro',
        price_monthly: 10,
        price_annually: 100,
        price_note_annual: 'Billed yearly, 2 months free',
        tagline: 'You and all your agents.',
        features: [],
      },
      {
        id: 'enterprise',
        name: 'Enterprise',
        price_monthly: null,
        price_annually: null,
        price_label: 'from $30k/yr',
        tagline: 'Dedicated or self-hosted, up to 100 users.',
        features: [],
      },
    ],
    comparison: {
      title: 'Compare plans',
      note: 'Governance never stops.',
      groups: [
        {
          title: 'Plan',
          rows: [
            {
              label: 'Agents',
              values: { free: '3', pro: 'Unlimited', enterprise: 'Unlimited' },
            },
          ],
        },
        {
          title: 'Optimize',
          rows: [
            {
              label: 'AI session optimization',
              values: { free: false, pro: true, enterprise: true },
            },
          ],
        },
      ],
    },
    faqs: [],
  },
};

function stubLadderFetch() {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(LADDER_CONTENT), { status: 200 });
      }
      return new Response(JSON.stringify({ features: {} }), { status: 200 });
    });
}

function stubFetch(features: Record<string, boolean> = { billing: false }) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(CONTENT), { status: 200 });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
}

describe('PublicPricingView', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub.restore();
  });

  it('preserves nested SSR cloud copy and separate self-hosted contact options', async () => {
    fetchStub = stubFetch();
    const deployments = [
      {
        title: 'Business Self-Hosted',
        description: 'Pilot by agreement',
        cta_text: 'Discuss a pilot',
        cta_url: '/request-demo',
      },
    ];
    const el = await fixture<PublicPricingView>(
      html`<public-pricing-view
        ><article>
          <header
            slot="pricing-heading"
            data-title="Cloud pricing"
            data-lead="USD, excluding tax. Self-hosted is separate."
            data-billing-toggle="false"
          ></header>
          <section>
            <div
              slot="plan-0"
              data-plan-id="free"
              data-plan-name="Free"
              data-price-monthly="0"
              data-price-annually="0"
            ></div>
          </section>
          <section
            slot="deployment-options"
            data-deployments=${JSON.stringify(deployments)}
          ></section></article
      ></public-pricing-view>`
    );
    await waitUntil(() => (el as any)._loaded);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent)
      .to.include('Cloud pricing')
      .and.include('USD, excluding tax. Self-hosted is separate.');
    expect(el.shadowRoot?.textContent)
      .to.include('Business Self-Hosted')
      .and.include('Pilot by agreement');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('landing-content.json'))
    ).to.equal(false);
  });

  it('loads plans from JSON and renders the hero copy and cards', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Simple Pricing');
    expect(el.shadowRoot?.textContent).to.contain('Pick a plan that fits.');
    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(2);
  });

  it('renders the FAQ section from JSON', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.pricing-faq')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Is there a trial?');
  });

  it('sends logged-out visitors to /register for the teams plan (card-free signup)', async () => {
    fetchStub = stubFetch({ billing: true, oauth_signin: true });
    localStorage.removeItem('accessToken');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    // The teams CTA never creates a checkout session for anonymous visitors:
    // signup is card-free; checkout is the in-product upgrade door.
    await (el as any)._handleSignUp('teams');
    const checkoutCalls = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('create-checkout-session'));
    expect(checkoutCalls.length).to.equal(0);
    expect(navStub.calledOnceWith('/register')).to.be.true;
  });

  it('renders all five-ladder cards and the comparison table below them', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(3);

    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table, 'comparison table renders').to.exist;
    // One header column per plan plus the leading label column.
    expect(table?.querySelectorAll('thead th').length).to.equal(4);
    expect(table?.textContent).to.contain('Agents');
    expect(table?.textContent).to.contain('Unlimited');
    expect(el.shadowRoot?.textContent).to.contain('Governance never stops.');
  });

  it('renders booleans in the comparison table as marks, not as text', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table?.querySelector('.check-mark'), 'included mark').to.exist;
    expect(table?.querySelector('.cross-mark'), 'excluded mark').to.exist;
    // "true"/"false" must never leak into the rendered copy.
    expect(table?.textContent).to.not.contain('true');
    expect(table?.textContent).to.not.contain('false');
  });

  it('never advertises a per-user unit: pricing is per bracket', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    for (const card of cards) {
      const text = (card as HTMLElement).shadowRoot?.textContent || '';
      expect(text, 'no per-seat unit on the card').to.not.contain('/user');
    }
  });

  it('routes existing accounts to comparison before any paid-plan mutation', async () => {
    fetchStub = stubLadderFetch();
    const el = await fixture<PublicPricingView>(
      html`<public-pricing-view></public-pricing-view>`
    );
    await waitUntil(() => (el as any)._loaded);
    localStorage.setItem('accessToken', 'test-token');
    const navigate = sinon.stub(el as any, '_navigate');
    await (el as any)._handleSignUp('pro');
    expect(
      navigate.calledOnceWith(
        '/console/settings/account?plan=pro&interval=year'
      )
    ).to.equal(true);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-checkout-session'))
    ).to.equal(false);
    localStorage.removeItem('accessToken');
  });

  it('routes an existing account considering Free to comparison, never checkout', async () => {
    fetchStub = stubLadderFetch();
    localStorage.setItem('accessToken', 'token');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    try {
      await (el as any)._handleSignUp('free');
      const checkoutCalls = fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('create-checkout-session'));
      expect(checkoutCalls.length).to.equal(0);
      expect(
        navStub.calledOnceWith(
          '/console/settings/account?plan=free&interval=year'
        )
      ).to.be.true;
    } finally {
      localStorage.removeItem('accessToken');
    }
  });

  it('sends enterprise to the contact route, never to checkout', async () => {
    fetchStub = stubLadderFetch();
    localStorage.setItem('accessToken', 'token');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    try {
      await (el as any)._handleSignUp('enterprise');
      const checkoutCalls = fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('create-checkout-session'));
      expect(checkoutCalls.length).to.equal(0);
      expect(navStub.calledOnceWith('/request-demo')).to.be.true;
    } finally {
      localStorage.removeItem('accessToken');
    }
  });

  it('rehydrates the comparison table from SSR slotted markup', async () => {
    fetchStub = stubLadderFetch();
    const payload = JSON.stringify(LADDER_CONTENT.pricing.comparison);
    const el = (await fixture(html`
      <public-pricing-view>
        <div
          slot="plan-0"
          data-plan-id="pro"
          data-plan-name="Pro"
          data-price-monthly="10"
          data-price-annually="100"
          data-tagline="You and all your agents."
          data-features=""
        ></div>
        <section slot="comparison" data-comparison=${payload}></section>
      </public-pricing-view>
    `)) as PublicPricingView;
    await tick();
    await el.updateComplete;

    // Slotted content wins over the JSON fetch, so the crawler-visible copy
    // is exactly what the visitor ends up interacting with.
    expect((el as any)._plans.length).to.equal(1);
    expect(el.shadowRoot?.querySelector('.comparison-table')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('AI session optimization');
  });

  it('survives a malformed comparison payload without losing the cards', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(html`
      <public-pricing-view>
        <div
          slot="plan-0"
          data-plan-id="pro"
          data-plan-name="Pro"
          data-price-monthly="10"
          data-price-annually="100"
          data-features=""
        ></div>
        <section slot="comparison" data-comparison="{not json"></section>
      </public-pricing-view>
    `)) as PublicPricingView;
    await tick();
    await el.updateComplete;

    expect((el as any)._comparison).to.equal(null);
    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(1);
  });

  it('falls back gracefully when content fails to load (no plans)', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/landing-content.json')) {
          return new Response('nope', { status: 500 });
        }
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      });
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._plans.length).to.equal(0);
    expect(el.shadowRoot?.querySelector('pricing-card')).to.not.exist;
  });
});
