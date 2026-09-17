/**
 * What the nudge says, when it comes back, and whose dismissal it is.
 *
 * The rules under test are the ones a person would notice if they broke: a
 * limit below half says nothing, a dismissed limit stays quiet until it
 * crosses the next band, and one browser shared by two people does not carry
 * one person's dismissals into the other's console.
 */
import { expect } from '@open-wc/testing';
import { NO_USAGE_NUDGES, type UsageNudge, type UsageNudges } from '../api';
import { router } from '../router';
import {
  FALLBACK_LADDER,
  NUDGE_BANDS,
  NUDGE_THRESHOLD,
  PLAN_ROUTE,
  PLAN_ROUTE_FALLBACK,
  analyticsWindow,
  bandFor,
  clearNudgeDismissals,
  dismissNudge,
  loadNudgeDismissals,
  nudgeDismissalsKey,
  nudgeLadder,
  nudgeLink,
  nudgeMessage,
  visibleNudges,
} from './usage-nudges';

function nudge(overrides: Partial<UsageNudge> = {}): UsageNudge {
  return {
    key: 'max_agents',
    ratio: 0.67,
    used: 2,
    limit: 3,
    unit: 'agents',
    plan_id: 'free',
    unlocks_at_plan: 'pro',
    ...overrides,
  };
}

function payload(overrides: Partial<UsageNudges> = {}): UsageNudges {
  return { ...NO_USAGE_NUDGES, ...overrides };
}

describe('usage nudge bands', () => {
  it('says nothing below half of a limit', () => {
    expect(bandFor(0.49)).to.equal(null);
    expect(NUDGE_THRESHOLD).to.equal(0.5);
    expect(NUDGE_BANDS).to.deep.equal([0.5, 0.8, 1.0]);
  });

  it('reports the band a ratio has reached, not the ratio', () => {
    expect(bandFor(0.5)).to.equal(0.5);
    expect(bandFor(0.79)).to.equal(0.5);
    expect(bandFor(0.8)).to.equal(0.8);
    expect(bandFor(1)).to.equal(1);
    expect(bandFor(2)).to.equal(1);
  });

  it('treats a nonsense ratio as nothing to say', () => {
    expect(bandFor(Number.NaN)).to.equal(null);
  });
});

describe('usage nudge dismissals', () => {
  const user = 'user-1';

  afterEach(() => {
    clearNudgeDismissals(user);
    clearNudgeDismissals('user-2');
  });

  it('stores one band per key, per user', () => {
    dismissNudge(user, 'max_agents', 0.5);
    expect(loadNudgeDismissals(user)).to.deep.equal({ max_agents: 0.5 });
    expect(loadNudgeDismissals('user-2')).to.deep.equal({});
    expect(nudgeDismissalsKey(user)).to.equal('preloop.nudges.user-1');
  });

  it('reads an unparseable or foreign record as nothing dismissed', () => {
    localStorage.setItem(nudgeDismissalsKey(user), 'not json');
    expect(loadNudgeDismissals(user)).to.deep.equal({});
    localStorage.setItem(
      nudgeDismissalsKey(user),
      JSON.stringify({ v: 99, bands: { max_agents: 0.5 } })
    );
    expect(loadNudgeDismissals(user)).to.deep.equal({});
  });

  it('hides a dismissed nudge and shows it again at the next band', () => {
    const dismissals = dismissNudge(user, 'max_agents', 0.5);
    expect(visibleNudges([nudge({ ratio: 0.67 })], dismissals)).to.have.length(
      0
    );
    expect(visibleNudges([nudge({ ratio: 0.85 })], dismissals)).to.have.length(
      1
    );
    expect(visibleNudges([nudge({ ratio: 1 })], dismissals)).to.have.length(1);
  });

  it('never shows a limit under the threshold, dismissed or not', () => {
    expect(visibleNudges([nudge({ ratio: 0.2 })], {})).to.have.length(0);
  });
});

describe('what the console is allowed to say', () => {
  it('gates on the threshold the server stated, not on this build bands', () => {
    // A server that nudges from 0.9 and publishes no bands is obeyed: the
    // console used to fall back to its own 0.5 and speak about limits the
    // server had decided were not worth a sentence.
    const strict = nudgeLadder(payload({ threshold: 0.9, bands: null }));
    expect(strict.threshold).to.equal(0.9);
    expect(visibleNudges([nudge({ ratio: 0.67 })], {}, strict)).to.have.length(
      0
    );
    expect(visibleNudges([nudge({ ratio: 0.95 })], {}, strict)).to.have.length(
      1
    );

    const early = nudgeLadder(payload({ threshold: 0.25, bands: null }));
    expect(visibleNudges([nudge({ ratio: 0.3 })], {}, early)).to.have.length(1);
  });

  it('records a dismissal below the first band at the threshold', () => {
    const early = nudgeLadder(payload({ threshold: 0.25, bands: null }));
    expect(bandFor(0.3, early.bands)).to.equal(null);
    const dismissals = { max_agents: early.threshold };
    expect(
      visibleNudges([nudge({ ratio: 0.3 })], dismissals, early)
    ).to.have.length(0);
    expect(
      visibleNudges([nudge({ ratio: 0.6 })], dismissals, early)
    ).to.have.length(1);
  });

  it('says nothing about a limit that is not a number it can state', () => {
    // Unlimited items are dropped server-side. If one arrives anyway, the
    // console stays quiet rather than printing "12 of -1 agents" or reaching
    // for a limit of its own.
    for (const limit of [-1, 0, Number.NaN]) {
      expect(
        visibleNudges([nudge({ ratio: 1, used: 12, limit })], {})
      ).to.have.length(0);
    }
  });

  it('prints an unknown plan id verbatim and invents nothing for it', () => {
    // The account pays for a plan that is not in the public ladder. Its
    // numbers are the server's, and no Free-plan limit is substituted.
    const legacy = nudge({
      key: 'analytics_window_days',
      ratio: 0.8219,
      used: 300,
      limit: 365,
      unit: 'days',
      plan_id: 'teams',
      unlocks_at_plan: 'team',
    });
    expect(visibleNudges([legacy], {}, FALLBACK_LADDER)).to.deep.equal([
      legacy,
    ]);
    expect(nudgeMessage(legacy)).to.equal(
      '300 of 365 days of analytics history'
    );
    expect(nudgeMessage(legacy)).to.not.contain('90');
    expect(nudgeLink(legacy.key)).to.contain('?feature=analytics_window_days');
  });

  it('keeps the analytics window of a plan it has never heard of', () => {
    const window = analyticsWindow(
      payload({
        analytics_window: {
          days: 365,
          unlocks_at_plan: 'team',
          unlocks_at_plan_name: null,
        },
      })
    );
    expect(window?.days).to.equal(365);
    expect(window?.unlocks_at_plan).to.equal('team');
  });
});

describe('usage nudge copy', () => {
  it('states the number, not an adjective', () => {
    expect(nudgeMessage(nudge())).to.equal('2 of 3 agents');
    expect(
      nudgeMessage(
        nudge({
          key: 'byok_ingest_tokens_monthly',
          used: 51_000_000,
          limit: 100_000_000,
          unit: 'tokens',
        })
      )
    ).to.equal('51M of 100M analysis tokens this month');
    expect(
      nudgeMessage(
        nudge({
          key: 'hosted_credit_one_time_usd',
          used: 0.26,
          limit: 0.5,
          unit: 'usd',
        })
      )
    ).to.equal('$0.26 of $0.50 built-in credit');
    expect(
      nudgeMessage(
        nudge({
          key: 'hosted_models_monthly_limit_usd',
          used: 1.2,
          limit: 2,
          unit: 'usd',
        })
      )
    ).to.equal('$1.20 of $2.00 built-in model spend this month');
  });

  it('states the analytics window as a window once data is behind it', () => {
    expect(
      nudgeMessage(
        nudge({
          key: 'analytics_window_days',
          ratio: 1,
          used: 90,
          limit: 90,
          unit: 'days',
        })
      )
    ).to.equal('Analytics goes back 90 days on your plan');
    expect(
      nudgeMessage(
        nudge({
          key: 'analytics_window_days',
          ratio: 0.7,
          used: 63,
          limit: 90,
          unit: 'days',
        })
      )
    ).to.equal('63 of 90 days of analytics history');
  });

  it('still says something for a limit this build has never heard of', () => {
    expect(
      nudgeMessage(
        nudge({ key: 'future_limit', used: 4, limit: 5, unit: 'widgets' })
      )
    ).to.equal('4 of 5 widgets');
  });
});

describe('usage nudge link', () => {
  it('names the feature so the plan page can open on it', () => {
    const link = nudgeLink('max_agents');
    expect(link).to.contain('?feature=max_agents');
    // The plan page is built alongside this banner; until its route exists
    // the account view is where plans have always lived.
    expect([PLAN_ROUTE, PLAN_ROUTE_FALLBACK]).to.contain(link.split('?')[0]);
  });

  // The seam #769 left for the plan page, now that the page has landed. The
  // test above runs with a bare router and so cannot tell the two answers
  // apart; these two register the route table either way and pin each answer.
  it('links to the plan page once its route is registered', async () => {
    await router.setRoutes(
      [
        {
          path: '/console',
          children: [{ path: 'settings/plan', component: 'plan-view' }],
        },
      ],
      true
    );
    try {
      expect(nudgeLink('max_agents')).to.equal(
        `${PLAN_ROUTE}?feature=max_agents`
      );
    } finally {
      await router.setRoutes([], true);
    }
  });

  it('falls back to the account view when only a catch-all matches', async () => {
    await router.setRoutes(
      [{ path: '(.*)', component: 'not-found-view' }],
      true
    );
    try {
      expect(nudgeLink('max_agents')).to.equal(
        `${PLAN_ROUTE_FALLBACK}?feature=max_agents`
      );
    } finally {
      await router.setRoutes([], true);
    }
  });
});

describe('the analytics window', () => {
  it('reads the window the payload states, with the plan that lifts it', () => {
    expect(
      analyticsWindow(
        payload({
          analytics_window: {
            days: 90,
            unlocks_at_plan: 'team',
            unlocks_at_plan_name: 'Team',
          },
        })
      )
    ).to.deep.equal({
      days: 90,
      unlocks_at_plan: 'team',
      unlocks_at_plan_name: 'Team',
    });
  });

  it('does not depend on the account being near any threshold', () => {
    // The row saying where history stops has to exist for an account with a
    // 90 day window and a week of data, which no nudge would ever mention.
    const quiet = payload({
      nudges: [nudge({ ratio: 0.01, used: 0 })],
      analytics_window: {
        days: 90,
        unlocks_at_plan: 'team',
        unlocks_at_plan_name: 'Team',
      },
    });
    expect(visibleNudges(quiet.nudges, {})).to.have.length(0);
    expect(analyticsWindow(quiet)?.days).to.equal(90);
  });

  it('is null when there is no window, which is the OSS answer', () => {
    expect(analyticsWindow(NO_USAGE_NUDGES)).to.equal(null);
    expect(analyticsWindow(payload({ nudges: [nudge()] }))).to.equal(null);
  });

  it('refuses a window that is not a usable number of days', () => {
    for (const days of [0, -30, Number.NaN]) {
      expect(
        analyticsWindow(
          payload({
            analytics_window: {
              days,
              unlocks_at_plan: null,
              unlocks_at_plan_name: null,
            },
          })
        )
      ).to.equal(null);
    }
  });
});

describe('the nudge ladder', () => {
  it('prefers the ladder the server publishes', () => {
    const ladder = nudgeLadder(payload({ threshold: 0.6, bands: [0.9, 0.6] }));
    expect(ladder.threshold).to.equal(0.6);
    expect(ladder.bands).to.deep.equal([0.6, 0.9]);
    expect(bandFor(0.7, ladder.bands)).to.equal(0.6);
    expect(bandFor(0.5, ladder.bands)).to.equal(null);
  });

  it('falls back to this build when the server states nothing', () => {
    const ladder = nudgeLadder(NO_USAGE_NUDGES);
    expect(ladder.threshold).to.equal(NUDGE_THRESHOLD);
    expect(ladder.bands).to.deep.equal(NUDGE_BANDS);
  });

  it('ignores a ladder that is not a ladder', () => {
    const ladder = nudgeLadder(
      payload({ threshold: Number.NaN, bands: [] as number[] })
    );
    expect(ladder.threshold).to.equal(NUDGE_THRESHOLD);
    expect(ladder.bands).to.deep.equal(NUDGE_BANDS);
  });
});
