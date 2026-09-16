import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './trial-prompt';
import { TrialPromptElement } from './trial-prompt';

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const OFFER = {
  show: true,
  reason: 'eligible',
  trial_days: 14,
  plans: [
    { id: 'pro', name: 'Pro', price_monthly: 12, price_annually: 120 },
    { id: 'team', name: 'Team', price_monthly: 120, price_annually: 1200 },
    {
      id: 'business',
      name: 'Business',
      price_monthly: 350,
      price_annually: 3600,
    },
  ],
};

describe('trial-prompt', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('billing/trial-prompt/dismiss')) {
        return new Response(JSON.stringify({ show: false }), { status: 200 });
      }
      if (url.includes('billing/trial-prompt')) {
        return new Response(JSON.stringify(OFFER), { status: 200 });
      }
      if (url.includes('create-checkout-session')) {
        return new Response(
          JSON.stringify({
            action: 'redirect',
            code: 'checkout_session_created',
            url: '#stripe-trial',
            message: 'Opening secure checkout for Pro.',
          }),
          { status: 200 }
        );
      }
      return new Response('{}', { status: 200 });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function mount(enabled: boolean): Promise<TrialPromptElement> {
    const el = (await fixture(
      html`<trial-prompt .enabled=${enabled}></trial-prompt>`
    )) as TrialPromptElement;
    await tick();
    await el.updateComplete;
    return el;
  }

  it('asks nothing and renders nothing when billing is off (OSS default)', async () => {
    // The OSS console has no billing plugin and no trial to sell. It must not
    // call the endpoint, and it must not render a step.
    const el = await mount(false);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('trial-prompt'))
    ).to.equal(false);
    expect(el.shadowRoot?.querySelector('sl-dialog')).to.not.exist;
  });

  it('offers the three paid plans and the free way out', async () => {
    const el = await mount(true);
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('14 days');
    expect(text).to.contain('A card is');
    expect(text).to.contain('returns to');
    const planButtons = Array.from(
      el.shadowRoot?.querySelectorAll('.plan-button') || []
    ).map((b) => b.getAttribute('data-plan'));
    expect(planButtons).to.deep.equal(['pro', 'team', 'business']);
    expect(el.shadowRoot?.querySelector('#continue-free')).to.exist;
  });

  it('uses no em dash in the trial copy (founder ruling)', async () => {
    const el = await mount(true);
    expect(el.shadowRoot?.textContent || '').to.not.contain('—');
  });

  it('records the answer before opening Stripe, so a cancel is not re-asked', async () => {
    const el = await mount(true);
    const originalHash = window.location.hash;
    try {
      const button = el.shadowRoot?.querySelector(
        '[data-plan="pro"]'
      ) as HTMLElement;
      button.click();
      await tick();
      const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
      const dismissAt = urls.findIndex((u) =>
        u.includes('trial-prompt/dismiss')
      );
      const checkoutAt = urls.findIndex((u) =>
        u.includes('create-checkout-session')
      );
      expect(dismissAt, 'the answer is recorded').to.be.greaterThan(-1);
      expect(checkoutAt, 'checkout opened').to.be.greaterThan(-1);
      expect(dismissAt).to.be.lessThan(checkoutAt);
      expect(window.location.hash).to.equal('#stripe-trial');
    } finally {
      window.location.hash = originalHash;
    }
  });

  it('records the answer and closes when the user continues on Free', async () => {
    const el = await mount(true);
    const button = el.shadowRoot?.querySelector(
      '#continue-free'
    ) as HTMLElement;
    button.click();
    await tick();
    await el.updateComplete;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('trial-prompt/dismiss'))
    ).to.equal(true);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-checkout-session'))
    ).to.equal(false);
    expect(el.shadowRoot?.querySelector('sl-dialog')).to.not.exist;
  });

  it('stays out of the way when the server says there is nothing to offer', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.resolves(
      new Response(
        JSON.stringify({
          show: false,
          reason: 'subscription_exists',
          trial_days: 14,
          plans: [],
        }),
        { status: 200 }
      )
    );
    const el = await mount(true);
    expect(el.shadowRoot?.querySelector('sl-dialog')).to.not.exist;
  });
});
