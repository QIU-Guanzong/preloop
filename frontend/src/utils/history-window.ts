/**
 * The analytics history window, as the console has to speak about it.
 *
 * A plan's analytics window is a read-time limit: older rows still exist
 * (physical retention is a separate promise, and audit and evidence records
 * follow their own), the plan simply does not show them. The backend states
 * that with 403 `analytics_history_unavailable` and the timestamp the window
 * starts at.
 *
 * Two rules shape everything here:
 *
 * 1. A refused period is not an error. "Failed to load" is a bug report; the
 *    honest sentence is that the period is outside the plan's window.
 * 2. The paywall modal follows a user action and nothing else. Asking for an
 *    older period, or pressing the link on the cutoff row, is an action.
 *    A background comparison fetch that happens to reach past the cutoff is
 *    not, and it degrades in silence.
 */

/** The backend's code for "this period is outside your analytics window". */
export const HISTORY_UNAVAILABLE_CODE = 'analytics_history_unavailable';

/** The feature key the plan page and the nudge banner use for the window. */
export const ANALYTICS_WINDOW_FEATURE = 'analytics_window_days';

/**
 * A refused history read, with the moment the plan's window starts.
 *
 * A distinct type rather than a message: a caller has to be able to tell a
 * plan limit from a server that fell over, and only one of those is worth
 * offering an upgrade for.
 */
export class HistoryUnavailableError extends Error {
  readonly code = HISTORY_UNAVAILABLE_CODE;
  /** ISO timestamp the visible window starts at, when the server sent one. */
  readonly availableFrom: string | null;

  constructor(message: string, availableFrom: string | null = null) {
    super(message);
    this.name = 'HistoryUnavailableError';
    this.availableFrom = availableFrom;
  }
}

/**
 * Turn a 403 that carries the history contract into that error, or null.
 *
 * Reads a clone so the caller can still consume the body, and treats an
 * unreadable body as "not this case": a generic 403 is a permission
 * problem, and offering to sell a plan for one is the wrong answer.
 */
export async function historyUnavailableError(
  response: Response
): Promise<HistoryUnavailableError | null> {
  if (response.status !== 403) return null;
  try {
    const body = await response.clone().json();
    const detail = body?.detail;
    if (!detail || detail.code !== HISTORY_UNAVAILABLE_CODE) return null;
    return new HistoryUnavailableError(
      String(
        detail.message || 'This period is outside your analytics history.'
      ),
      detail.available_from ? String(detail.available_from) : null
    );
  } catch {
    return null;
  }
}

/** Whether a caught value is a refused history read. */
export function isHistoryUnavailable(
  error: unknown
): error is HistoryUnavailableError {
  return error instanceof HistoryUnavailableError;
}

/**
 * The sentence shown where the data stops.
 *
 * Every finite window belongs to some plan, including the paid ones, so the
 * sentence names the plan that actually lifts this window rather than
 * claiming older data "is on paid plans": telling someone who already pays
 * that their history is on paid plans is both false and the fastest way to
 * make them distrust the rest of the console. At the top of the ladder there
 * is no plan to name, so the row says nothing about plans at all.
 */
export function historyCutoffMessage(
  days: number,
  unlocksAtPlanName?: string | null
): string {
  const older = `Data older than ${Math.round(days)} days`;
  return unlocksAtPlanName
    ? `${older} is on the ${unlocksAtPlanName} plan and above`
    : `${older} is outside your plan's analytics window`;
}

/**
 * Open the existing upgrade modal for the analytics window.
 *
 * Only ever called from a user action: a range the person chose, or the link
 * on the cutoff row. The modal contract is the one `api.ts` dispatches for a
 * 402, so the shell needs no second handler.
 */
export function requestHistoryUpgrade(): void {
  window.dispatchEvent(
    new CustomEvent('show-upgrade-modal', {
      detail: {
        feature: ANALYTICS_WINDOW_FEATURE,
        code: 'upgrade_required',
      },
      bubbles: true,
      composed: true,
    })
  );
}
