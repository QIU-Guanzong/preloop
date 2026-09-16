import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { cloudPlans, loadPricingContent } from './pricing-content';

describe('pricing content', () => {
  let fetchStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  afterEach(() => {
    fetchStub?.restore();
  });

  it('reads the published ladder the public page reads', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () =>
      json({
        pricing: {
          title: 'Pricing',
          lead: 'Start free.',
          plans: [
            {
              id: 'free',
              name: 'Free',
              price_monthly: 0,
              price_annually: 0,
              features: ['One'],
            },
            {
              id: 'enterprise-selfhosted',
              name: 'Enterprise',
              deployment: 'dedicated',
              price_monthly: null,
              price_annually: null,
            },
          ],
          comparison: { groups: [{ title: 'Plan', rows: [] }] },
          dedicated: {
            plans: [{ id: 'opensource', name: 'Open Source' }],
          },
          faqs: [{ q: 'Is there a trial?', a: 'Yes.' }],
        },
      })
    );

    const content = await loadPricingContent();
    expect(content.title).to.equal('Pricing');
    expect(content.plans).to.have.length(2);
    expect(content.comparison?.groups).to.have.length(1);
    expect(content.dedicatedPlans[0].deployment).to.equal('dedicated');
    expect(content.faqs).to.have.length(1);
  });

  it('offers the console only what a console can buy', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () =>
      json({
        pricing: {
          plans: [
            { id: 'free', name: 'Free' },
            { id: 'onprem', name: 'On premises', deployment: 'dedicated' },
          ],
        },
      })
    );

    const content = await loadPricingContent();
    // A self-hosted edition is not a subscription, so the plan page must not
    // offer it beside the hosted ladder.
    expect(cloudPlans(content).map((p) => p.id)).to.deep.equal(['free']);
  });

  it('refuses to invent prices when the content cannot be read', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response('', { status: 404 }));

    let failed = false;
    try {
      await loadPricingContent();
    } catch {
      failed = true;
    }
    // A page that cannot say what a plan costs has to say so.
    expect(failed).to.equal(true);
  });
});
