/**
 * The banner that replaced the first-load upgrade prompt.
 *
 * What is being proven: it speaks only about limits the account is at or
 * past half of, it says the number, it stays dismissed until the next band,
 * it never opens a modal by itself, and on OSS - where the endpoint does not
 * exist and answers 404 - it renders nothing at all.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './usage-nudge-banner.ts';
import type { UsageNudgeBanner } from './usage-nudge-banner.ts';
import type { UsageNudge } from '../api';
import {
  clearNudgeDismissals,
  loadNudgeDismissals,
  nudgeDismissalsKey,
} from '../utils/usage-nudges';

const USER = { id: 'user-1', account_id: 'acct-1', username: 'jdoe' };

const AGENTS: UsageNudge = {
  key: 'max_agents',
  ratio: 0.67,
  used: 2,
  limit: 3,
  unit: 'agents',
  plan_id: 'free',
  unlocks_at_plan: 'pro',
};

const TOKENS: UsageNudge = {
  key: 'byok_ingest_tokens_monthly',
  ratio: 0.51,
  used: 51_000_000,
  limit: 100_000_000,
  unit: 'tokens',
  plan_id: 'free',
  unlocks_at_plan: 'pro',
};

const QUIET: UsageNudge = { ...AGENTS, ratio: 0.1, used: 0 };

describe('usage-nudge-banner', () => {
  let restoreFetch: (() => void) | undefined;
  let nudgeRequests = 0;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    nudgeRequests = 0;
  });

  afterEach(() => {
    restoreFetch?.();
    restoreFetch = undefined;
    clearNudgeDismissals(USER.id);
    localStorage.removeItem('accessToken');
  });

  function stubApi(nudges: UsageNudge[] | 'missing') {
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/billing/nudges')) {
        nudgeRequests += 1;
        if (nudges === 'missing') {
          // OSS: no billing plugin, so no such route.
          return new Response(JSON.stringify({ detail: 'Not Found' }), {
            status: 404,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify(nudges), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/auth/users/me')) {
        return new Response(JSON.stringify(USER), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return original(input, init);
    }) as typeof window.fetch;
    restoreFetch = () => {
      window.fetch = original;
    };
  }

  async function mount(
    nudges: UsageNudge[] | 'missing'
  ): Promise<UsageNudgeBanner> {
    stubApi(nudges);
    const el = await fixture<UsageNudgeBanner>(
      html`<usage-nudge-banner></usage-nudge-banner>`
    );
    await waitUntil(() => nudgeRequests > 0);
    await el.updateComplete;
    // The profile and the list land in the same pass; let it settle.
    await el.updateComplete;
    return el;
  }

  it('renders nothing on OSS, where the endpoint does not exist', async () => {
    const el = await mount('missing');
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
  });

  it('renders nothing while every limit is under half', async () => {
    const el = await mount([QUIET]);
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
  });

  it('states each limit with its number and links to plans', async () => {
    const el = await mount([AGENTS, TOKENS]);
    await waitUntil(() => !!el.shadowRoot?.querySelector('.row'));
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.contain('2 of 3 agents');
    expect(text).to.contain('51M of 100M analysis tokens this month');
    const link = el.shadowRoot!.querySelector<HTMLAnchorElement>('a.plans')!;
    expect(link.getAttribute('href')).to.contain('feature=max_agents');
  });

  it('never opens the upgrade modal from its own fetch', async () => {
    const seen: Event[] = [];
    const handler = (event: Event) => seen.push(event);
    window.addEventListener('show-upgrade-modal', handler);
    await mount([AGENTS]);
    window.removeEventListener('show-upgrade-modal', handler);
    expect(seen).to.have.length(0);
  });

  it('keeps a dismissed limit quiet until it crosses the next band', async () => {
    const el = await mount([AGENTS]);
    await waitUntil(() => !!el.shadowRoot?.querySelector('.row'));
    el.shadowRoot!.querySelector<HTMLButtonElement>('button.dismiss')!.click();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.row')).to.equal(null);
    expect(loadNudgeDismissals(USER.id)).to.deep.equal({ max_agents: 0.5 });

    // The same limit, now at 80 percent, is news again.
    const louder = await mount([{ ...AGENTS, ratio: 0.9, used: 2.7 }]);
    await waitUntil(() => !!louder.shadowRoot?.querySelector('.row'));
    expect(louder.shadowRoot!.textContent).to.contain('agents');
  });

  it('re-checks at sign-in, so a second account does not read the first', async () => {
    const el = await mount([AGENTS]);
    await waitUntil(() => nudgeRequests === 1);
    window.dispatchEvent(new CustomEvent('auth-change'));
    await waitUntil(() => nudgeRequests > 1);
    await el.updateComplete;
    expect(nudgeRequests).to.be.greaterThan(1);
  });

  it('ignores a dismissal record another user wrote', async () => {
    localStorage.setItem(
      nudgeDismissalsKey('user-2'),
      JSON.stringify({ v: 1, bands: { max_agents: 1 } })
    );
    const el = await mount([AGENTS]);
    await waitUntil(() => !!el.shadowRoot?.querySelector('.row'));
    expect(el.shadowRoot!.textContent).to.contain('2 of 3 agents');
    localStorage.removeItem(nudgeDismissalsKey('user-2'));
  });
});
