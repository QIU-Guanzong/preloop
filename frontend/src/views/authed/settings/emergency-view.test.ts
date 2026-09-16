import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { invalidateApiCaches } from '../../../api';
import './emergency-view';
import type { EmergencyView } from './emergency-view';

describe('EmergencyView', () => {
  let fetchStub: sinon.SinonStub;

  function copy(el: EmergencyView): string {
    return (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(status: Record<string, unknown> | null = null) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/account/kill-switch/status')) {
          return json(status ?? { active: false, scopes: [] });
        }
        if (url.includes('/account/kill-switch/activate')) {
          return json({
            active: true,
            scopes: [
              { scope: 'gateway', reason: 'Runaway agent' },
              { scope: 'tools', reason: 'Runaway agent' },
              { scope: 'flows', reason: 'Runaway agent' },
            ],
          });
        }
        if (url.includes('/account/kill-switch/deactivate')) {
          return json({ active: false, scopes: [] });
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('offers the halt on a page of its own', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('Emergency Controls');
    expect(text).to.contain('Block new agent requests');
    // Nothing on this page is about a subscription.
    expect(text).to.not.contain('Manage in Stripe');
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/api/v1/billing/'))
    ).to.equal(false);
  });

  it('halts with the recorded reason', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await element.updateComplete;

    (element as any)._haltReason = 'Runaway agent';
    await (element as any)._handleHalt();
    await element.updateComplete;

    const request = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/kill-switch/activate'));
    expect(request, 'expected an activation request').to.exist;
    expect(JSON.parse(request!.args[1].body)).to.deep.equal({
      reason: 'Runaway agent',
    });
    expect(copy(element)).to.contain('Agent activity is halted');
  });

  it('includes the operator recovery reason in the deactivation request', async () => {
    fetchStub = createFetchStub({
      active: true,
      scopes: [{ scope: 'flows', reason: 'Inspect active runtimes' }],
    });
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await waitUntil(() => (element as any)._haltStatus !== null, 'status');
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelectorAll('sl-input[label="Recovery reason"]')
    ).to.have.length(1);
    (element as any)._haltReason = 'Runtime termination verified';
    await (element as any)._handleResume(['flows']);

    const request = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/kill-switch/deactivate'));
    expect(request).to.exist;
    expect(JSON.parse(request!.args[1].body)).to.deep.equal({
      scopes: ['flows'],
      reason: 'Runtime termination verified',
    });
  });

  it('keeps the last known state when a status read fails', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
      return new Response('{}', { status: 500 });
    });
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await element.updateComplete;

    // A failed read must not claim the account is halted, nor blank the page.
    expect(copy(element)).to.contain('Block new agent requests');
    expect(copy(element)).to.not.contain('Agent activity is halted');
  });
});
