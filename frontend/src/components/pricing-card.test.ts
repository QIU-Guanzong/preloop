import { html, fixture, expect } from '@open-wc/testing';
import './pricing-card';
import { PricingCard } from './pricing-card';

const BASE = {
  id: 'pro',
  name: 'Pro',
  price_monthly: 10,
  price_annually: 100,
  features: [] as string[],
};

async function renderCard(plan: any, interval: 'month' | 'year' = 'month') {
  const el = (await fixture(
    html`<pricing-card .plan=${plan} .interval=${interval}></pricing-card>`
  )) as PricingCard;
  await el.updateComplete;
  return el;
}

describe('PricingCard', () => {
  it('prices per bracket, never per seat', async () => {
    const el = await renderCard(BASE);
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('$10');
    expect(text).to.contain('/mo');
    // Bracket pricing: a plan costs the same for everyone inside the bracket,
    // so a "/user" unit would misstate what the customer is buying.
    expect(text).to.not.contain('/user');
  });

  it('switches to the annual price and its own note', async () => {
    const el = await renderCard(
      { ...BASE, price_note_annual: 'Billed yearly, 2 months free' },
      'year'
    );
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('$100');
    expect(text).to.contain('/yr');
    expect(text).to.contain('Billed yearly, 2 months free');
    // The saving is stated by config copy, never derived into a second
    // monthly-looking number.
    expect(text).to.not.contain('/mo');
  });

  it('honours price_label so a floor price is not shown as an exact one', async () => {
    const el = await renderCard({
      id: 'enterprise',
      name: 'Enterprise',
      price_monthly: null,
      price_annually: null,
      price_label: 'from $30k/yr',
      features: [],
    });
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('from $30k/yr');
    expect(text).to.not.contain('Custom');
  });

  it('renders a tagline instead of a feature list when one is set', async () => {
    const el = await renderCard({
      ...BASE,
      tagline: 'You and all your agents.',
      features: ['Should not render'],
    });
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('You and all your agents.');
    // One number plus one line: quota and feature rows belong in the table.
    expect(text).to.not.contain('Should not render');
    expect(el.shadowRoot?.querySelector('.features')).to.not.exist;
  });

  it('still renders a feature list for plans that have no tagline', async () => {
    const el = await renderCard({ ...BASE, features: ['Feature A'] });
    expect(el.shadowRoot?.textContent).to.contain('Feature A');
    expect(el.shadowRoot?.querySelector('.features')).to.exist;
  });

  it('shows $0 rather than the word Free so the ladder reads consistently', async () => {
    const el = await renderCard({
      id: 'free',
      name: 'Free',
      price_monthly: 0,
      price_annually: 0,
      tagline: 'Try Preloop with your own keys.',
      features: [],
    });
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('$0');
    // The free plan keeps its name heading like every other card.
    expect(text).to.contain('Free');
  });

  it('emits the plan id it was given when the CTA is pressed', async () => {
    const el = await renderCard({ ...BASE, id: 'business', name: 'Business' });
    let planId = '';
    el.addEventListener('signup-requested', (e) => {
      planId = (e as CustomEvent).detail.planId;
    });
    (el.shadowRoot?.querySelector('.cta') as HTMLElement).click();
    expect(planId).to.equal('business');
  });

  it('uses the configured CTA label when one is supplied', async () => {
    const el = await renderCard({ ...BASE, cta_text: 'Contact us' });
    expect(el.shadowRoot?.querySelector('.cta')?.textContent).to.contain(
      'Contact us'
    );
  });
});
