/**
 * The analytics window contract on the client.
 *
 * A refused period has to be distinguishable from a broken server, and the
 * upgrade modal has to stay attached to a user action.
 */
import { expect } from '@open-wc/testing';
import {
  HISTORY_UNAVAILABLE_CODE,
  HistoryUnavailableError,
  historyCutoffMessage,
  historyUnavailableError,
  isHistoryUnavailable,
  requestHistoryUpgrade,
} from './history-window';

function refusal(): Response {
  return new Response(
    JSON.stringify({
      detail: {
        code: HISTORY_UNAVAILABLE_CODE,
        available_from: '2026-06-18T00:00:00+00:00',
        message: "This period is outside your plan's analytics history.",
      },
    }),
    { status: 403, headers: { 'Content-Type': 'application/json' } }
  );
}

describe('history window', () => {
  it('recognises the refusal and keeps the server sentence', async () => {
    const error = await historyUnavailableError(refusal());
    expect(error).to.be.instanceOf(HistoryUnavailableError);
    expect(error!.availableFrom).to.equal('2026-06-18T00:00:00+00:00');
    expect(error!.message).to.contain('analytics history');
    expect(isHistoryUnavailable(error)).to.equal(true);
  });

  it('leaves the response body readable for the caller', async () => {
    const response = refusal();
    await historyUnavailableError(response);
    const body = await response.json();
    expect(body.detail.code).to.equal(HISTORY_UNAVAILABLE_CODE);
  });

  it('is not a plain 403: a permission problem is not a plan problem', async () => {
    const forbidden = new Response(JSON.stringify({ detail: 'Forbidden' }), {
      status: 403,
      headers: { 'Content-Type': 'application/json' },
    });
    expect(await historyUnavailableError(forbidden)).to.equal(null);
    expect(
      await historyUnavailableError(new Response('', { status: 500 }))
    ).to.equal(null);
    expect(isHistoryUnavailable(new Error('boom'))).to.equal(false);
  });

  it('names the plan that actually lifts this window', () => {
    expect(historyCutoffMessage(90, 'Team')).to.equal(
      'Data older than 90 days is on the Team plan and above'
    );
  });

  it('says nothing about plans when there is no higher plan', () => {
    // A paid plan with a finite window must never be told its own history
    // "is on paid plans": it is already on one, and there is nothing to buy.
    for (const unlocks of [null, undefined, '']) {
      expect(historyCutoffMessage(730, unlocks)).to.equal(
        "Data older than 730 days is outside your plan's analytics window"
      );
    }
  });

  it('opens the existing upgrade modal with the window named', async () => {
    const seen: CustomEvent[] = [];
    const handler = (event: Event) => seen.push(event as CustomEvent);
    window.addEventListener('show-upgrade-modal', handler);
    requestHistoryUpgrade();
    window.removeEventListener('show-upgrade-modal', handler);
    expect(seen).to.have.length(1);
    expect(seen[0].detail).to.deep.equal({
      feature: 'analytics_window_days',
      code: 'upgrade_required',
    });
  });
});
