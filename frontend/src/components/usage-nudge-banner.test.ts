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

/**
 * A paying account on a per-seat plan that is not in the public ladder.
 *
 * Its plan grants unlimited agents and unlimited analysis tokens, so the
 * server sends no item for either, and a year of analytics history, which it
 * has used an eighth of. The whole answer is therefore "nothing to say".
 */
const LEGACY_PLAN_PAYLOAD = {
  nudges: [
    {
      key: 'analytics_window_days',
      ratio: 0.1338,
      used: 48.85,
      limit: 365.0,
      unit: 'days',
      plan_id: 'teams',
      unlocks_at_plan: 'team',
    },
  ],
  analytics_window: { days: 365, unlocks_at_plan: 'team' },
  threshold: 0.5,
  bands: [0.5, 0.8, 1.0],
};

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

  /** Serve one exact response body on `/billing/nudges`, as the server sent it. */
  function stubBody(body: unknown) {
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/billing/nudges')) {
        nudgeRequests += 1;
        return new Response(JSON.stringify(body), {
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

  /**
   * Wait for the answer to have been applied, not merely requested.
   *
   * `refresh()` sets the caller's id and the payload in one block, the id
   * first, so an id on the element is proof that the nudges beside it are the
   * ones this stub served. Asserting "nothing is on screen" before that point
   * would pass for a banner that had simply not rendered yet.
   */
  async function settle(el: UsageNudgeBanner): Promise<void> {
    await waitUntil(
      () => (el as unknown as { userId: string }).userId === USER.id,
      'the nudge fetch never settled'
    );
    await el.updateComplete;
  }

  async function mountBody(body: unknown): Promise<UsageNudgeBanner> {
    stubBody(body);
    const el = await fixture<UsageNudgeBanner>(
      html`<usage-nudge-banner></usage-nudge-banner>`
    );
    await waitUntil(() => nudgeRequests > 0);
    await settle(el);
    return el;
  }

  function stubApi(
    nudges: UsageNudge[] | 'missing',
    ladder: { threshold: number; bands: number[] } | null = null
  ) {
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
        return new Response(
          JSON.stringify({
            nudges,
            analytics_window: null,
            threshold: ladder?.threshold ?? 0.5,
            bands: ladder?.bands ?? [0.5, 0.8, 1.0],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
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
    nudges: UsageNudge[] | 'missing',
    ladder: { threshold: number; bands: number[] } | null = null
  ): Promise<UsageNudgeBanner> {
    stubApi(nudges, ladder);
    const el = await fixture<UsageNudgeBanner>(
      html`<usage-nudge-banner></usage-nudge-banner>`
    );
    await waitUntil(() => nudgeRequests > 0);
    // The profile and the list land in the same pass; let it settle.
    await settle(el);
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

  it('nudges on the ladder the server publishes, not a second copy', async () => {
    // One ladder: if the server moves the threshold to 0.9, a limit at 0.67
    // says nothing, and a dismissal records the server's band.
    const quiet = await mount([AGENTS], { threshold: 0.9, bands: [0.9, 1.0] });
    expect(quiet.shadowRoot?.querySelector('.row')).to.equal(null);

    const loud = await mount([{ ...AGENTS, ratio: 0.95, used: 2.85 }], {
      threshold: 0.9,
      bands: [0.9, 1.0],
    });
    await waitUntil(() => !!loud.shadowRoot?.querySelector('.row'));
    loud
      .shadowRoot!.querySelector<HTMLButtonElement>('button.dismiss')!
      .click();
    await loud.updateComplete;
    expect(loadNudgeDismissals(USER.id)).to.deep.equal({ max_agents: 0.9 });
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

  // A paying account on a plan that is not on sale, and not in the public
  // ladder this build knows about. Every number on screen has to be one the
  // server sent about this account, or there must be no screen.
  describe('a plan the public ladder does not carry', () => {
    it('says nothing the server did not say', async () => {
      const el = await mountBody(LEGACY_PLAN_PAYLOAD);
      const text = el.shadowRoot?.textContent ?? '';

      // Unlimited agents and unlimited tokens: the server sent no item, so
      // there is no row, and in particular no Free-plan "3 agents" or
      // "100M tokens" invented on this side.
      expect(
        el.shadowRoot?.querySelector('[data-nudge="max_agents"]')
      ).to.equal(null);
      expect(
        el.shadowRoot?.querySelector(
          '[data-nudge="byok_ingest_tokens_monthly"]'
        )
      ).to.equal(null);
      expect(text).to.not.contain('agents');
      expect(text).to.not.contain('analysis tokens');

      // The one item the server did send sits below its own threshold, so it
      // says nothing either, and its limit is never restated as 90 days.
      expect(
        el.shadowRoot?.querySelector('[data-nudge="analytics_window_days"]')
      ).to.equal(null);
      expect(text).to.not.contain('90');
      expect(text.trim()).to.equal('');
    });

    it('prints the server numbers verbatim once one crosses the threshold', async () => {
      // The same account, later in the year: 300 of its own 365 days. The
      // limit on screen is the plan's, never the public ladder's.
      const el = await mountBody({
        ...LEGACY_PLAN_PAYLOAD,
        nudges: [
          {
            ...LEGACY_PLAN_PAYLOAD.nudges[0],
            ratio: 0.8219,
            used: 300,
          },
        ],
      });
      await waitUntil(() => !!el.shadowRoot?.querySelector('.row'));
      const text = el.shadowRoot!.textContent ?? '';
      expect(text).to.contain('300 of 365 days of analytics history');
      expect(text).to.not.contain('90');
      // An unknown plan id changes no number and still leads to the plans.
      const link = el.shadowRoot!.querySelector<HTMLAnchorElement>('a.plans')!;
      expect(link.getAttribute('href')).to.contain(
        'feature=analytics_window_days'
      );
    });
  });

  it('renders nothing at all when every limit is unlimited', async () => {
    // Unlimited limits are dropped server-side, so the answer is an empty
    // list with no window: one of those is the whole banner's input.
    const el = await mountBody({
      nudges: [],
      analytics_window: null,
      threshold: 0.5,
      bands: [0.5, 0.8, 1.0],
    });
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
    expect(el.shadowRoot?.querySelector('.banner')).to.equal(null);
  });

  it('stays quiet when an unlimited limit arrives as a number anyway', async () => {
    // A server that sends `-1` (or a zero allowance) rather than dropping the
    // item must not make this build print "3 of -1 agents", and must not make
    // it reach for a limit of its own to print instead.
    const el = await mountBody({
      nudges: [
        { ...AGENTS, ratio: 1, used: 12, limit: -1, plan_id: 'teams' },
        { ...TOKENS, ratio: 1, used: 318_900_000, limit: 0, plan_id: 'teams' },
      ],
      analytics_window: null,
      threshold: 0.5,
      bands: [0.5, 0.8, 1.0],
    });
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
  });

  it('nudges from the threshold the server states, with no bands sent', async () => {
    // Bands are the re-nudge ladder; the threshold is the gate. A server that
    // publishes only a threshold is obeyed rather than overridden by this
    // build's first band.
    const quiet = await mountBody({
      nudges: [{ ...AGENTS, ratio: 0.67 }],
      analytics_window: null,
      threshold: 0.9,
      bands: null,
    });
    expect(quiet.shadowRoot?.querySelector('.row')).to.equal(null);

    const early = await mountBody({
      nudges: [{ ...AGENTS, ratio: 0.3, used: 1 }],
      analytics_window: null,
      threshold: 0.25,
      bands: null,
    });
    await waitUntil(() => !!early.shadowRoot?.querySelector('.row'));
    expect(early.shadowRoot!.textContent).to.contain('1 of 3 agents');
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
