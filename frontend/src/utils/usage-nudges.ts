/**
 * The usage nudge: what it says, where it links, and when it comes back.
 *
 * The console used to ask for money on first load, which is a question the
 * person has not asked and cannot answer yet. A nudge instead states one
 * fact, with the number in it ("2 of 3 agents"), once the account is at or
 * past half of a limit it could outgrow, and it links to the plan page with
 * the limit named so the page can open on the right row.
 *
 * Nothing here opens a modal. The paywall modal belongs to a user action
 * (a price override, a hosted-model optimization, asking for older data);
 * a banner that arrives from a background fetch is the thing being
 * replaced.
 *
 * Dismissal is a per-person, per-device view preference like
 * `preloop.<collection>.view_mode`, stored under `preloop.nudges.<user id>`
 * because the backend has no generic per-user preference store to put it
 * in. Keying by user id keeps two accounts on one browser apart. What is
 * stored is the band the person dismissed at, not a boolean, so crossing
 * the next threshold (50, then 80, then 100 percent) says it again: "you
 * are at 80 percent" is new information, "you are still at 51" is not.
 */

import type { UsageNudge } from '../api';
import { router } from '../router';
import { formatTokenCount } from './execution-presentation';
import { ANALYTICS_WINDOW_FEATURE } from './history-window';

/** Below this ratio there is nothing worth saying. Mirrors the server. */
export const NUDGE_THRESHOLD = 0.5;

/** The ladder a dismissed nudge climbs back over. Mirrors the server. */
export const NUDGE_BANDS: readonly number[] = [0.5, 0.8, 1.0];

/** Where the plan page lives, and where it lived before it existed. */
export const PLAN_ROUTE = '/console/settings/plan';
export const PLAN_ROUTE_FALLBACK = '/console/settings/account';

/** Stored record version: bumped if the shape below ever changes. */
export const NUDGE_DISMISSALS_VERSION = 1;

/** Dismissed bands by nudge key, for one user. */
export type NudgeDismissals = Record<string, number>;

interface StoredDismissals {
  v: number;
  bands: NudgeDismissals;
}

/** `preloop.nudges.<user id>`, one record per person per browser. */
export function nudgeDismissalsKey(userId: string): string {
  return `preloop.nudges.${userId}`;
}

/**
 * The band a ratio has reached, or null below the threshold.
 *
 * A band, not the raw ratio: 0.51 and 0.79 are the same news, and a nudge
 * that reappears for every percent is an alarm nobody reads.
 */
export function bandFor(ratio: number): number | null {
  if (!Number.isFinite(ratio)) return null;
  let band: number | null = null;
  for (const step of NUDGE_BANDS) {
    if (ratio >= step) band = step;
  }
  return band;
}

/** Read this user's dismissals. Any failure reads as "nothing dismissed". */
export function loadNudgeDismissals(userId: string): NudgeDismissals {
  if (!userId) return {};
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(nudgeDismissalsKey(userId));
  } catch {
    return {};
  }
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== 'object') return {};
  const record = parsed as Partial<StoredDismissals>;
  if (record.v !== NUDGE_DISMISSALS_VERSION) return {};
  const bands = record.bands;
  if (!bands || typeof bands !== 'object') return {};
  const clean: NudgeDismissals = {};
  for (const [key, value] of Object.entries(bands)) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      clean[key] = value;
    }
  }
  return clean;
}

/**
 * Record that this user dismissed `key` while it stood at `band`.
 *
 * Storage failures are ignored: the banner closes either way, it simply
 * comes back on the next load, which is a smaller harm than a console that
 * breaks in private browsing.
 */
export function dismissNudge(
  userId: string,
  key: string,
  band: number
): NudgeDismissals {
  const next = { ...loadNudgeDismissals(userId), [key]: band };
  if (!userId) return next;
  try {
    const record: StoredDismissals = {
      v: NUDGE_DISMISSALS_VERSION,
      bands: next,
    };
    localStorage.setItem(nudgeDismissalsKey(userId), JSON.stringify(record));
  } catch {
    // A preference, not a requirement (see `utils/view-mode.ts`).
  }
  return next;
}

/** Forget every dismissal for a user. Used when a plan change resets the view. */
export function clearNudgeDismissals(userId: string): void {
  if (!userId) return;
  try {
    localStorage.removeItem(nudgeDismissalsKey(userId));
  } catch {
    // Same as writing: storage is optional.
  }
}

/**
 * The nudges worth showing: at or past the threshold, and either never
 * dismissed or dismissed at a lower band than they now stand at.
 */
export function visibleNudges(
  nudges: readonly UsageNudge[],
  dismissals: NudgeDismissals
): UsageNudge[] {
  return nudges.filter((nudge) => {
    const band = bandFor(nudge.ratio);
    if (band === null) return false;
    const dismissed = dismissals[nudge.key];
    return dismissed === undefined || band > dismissed;
  });
}

/**
 * Whether the plan page exists in this build.
 *
 * The link target is the plan page, which is being built alongside this
 * banner. Asking the router keeps one link correct in both builds: a path
 * that only the catch-all matches is not a page, so the account view (which
 * has carried plans and the current subscription all along) takes the link
 * until the plan page lands.
 */
export function planRouteExists(): boolean {
  try {
    const matched = router.match(PLAN_ROUTE);
    if (!matched || matched.chain.length === 0) return false;
    const leaf = matched.chain[matched.chain.length - 1];
    return Boolean(leaf.component) && leaf.component !== 'not-found-view';
  } catch {
    return false;
  }
}

/** `/console/settings/plan?feature=<key>`, or the account view before it exists. */
export function nudgeLink(key: string): string {
  const base = planRouteExists() ? PLAN_ROUTE : PLAN_ROUTE_FALLBACK;
  return `${base}?feature=${encodeURIComponent(key)}`;
}

/**
 * The plan's analytics window in days, from the nudge list, or null when the
 * account has no window (OSS answers with no nudges at all, and an unlimited
 * window is omitted server-side).
 */
export function analyticsWindowDays(
  nudges: readonly UsageNudge[]
): number | null {
  const entry = nudges.find((nudge) => nudge.key === ANALYTICS_WINDOW_FEATURE);
  if (!entry || !Number.isFinite(entry.limit) || entry.limit <= 0) return null;
  return entry.limit;
}

/** Money the way the console states money: two decimals, four under a cent. */
function money(amount: number): string {
  if (!Number.isFinite(amount) || amount === 0) return '$0.00';
  return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
}

/** A whole count, rounded: half an agent is not a thing. */
function whole(amount: number): string {
  return String(Math.round(Number.isFinite(amount) ? amount : 0));
}

/**
 * The one line the banner states.
 *
 * Plain, and with the number in it, because "you are approaching a limit"
 * tells nobody whether to act. An unknown key still gets a sentence rather
 * than a blank banner: a server that grows a sixth limit must not make this
 * build render an empty row.
 */
export function nudgeMessage(nudge: UsageNudge): string {
  switch (nudge.key) {
    case 'max_agents':
      return `${whole(nudge.used)} of ${whole(nudge.limit)} agents`;
    case 'byok_ingest_tokens_monthly':
      return `${formatTokenCount(nudge.used)} of ${formatTokenCount(
        nudge.limit
      )} analysis tokens this month`;
    case 'hosted_credit_one_time_usd':
      return `${money(nudge.used)} of ${money(nudge.limit)} built-in credit`;
    case 'hosted_models_monthly_limit_usd':
      return `${money(nudge.used)} of ${money(
        nudge.limit
      )} built-in model spend this month`;
    case ANALYTICS_WINDOW_FEATURE:
      return nudge.ratio >= 1
        ? `Analytics goes back ${whole(nudge.limit)} days on your plan`
        : `${whole(nudge.used)} of ${whole(
            nudge.limit
          )} days of analytics history`;
    default:
      return `${whole(nudge.used)} of ${whole(nudge.limit)} ${nudge.unit}`;
  }
}
