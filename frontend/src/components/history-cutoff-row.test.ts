/**
 * The row where a plan's analytics window ends.
 *
 * A list that stops without saying why reads as "nothing happened", so the
 * row states the gap. With no window (OSS, or an unlimited plan) there is no
 * row at all, and the row itself only reaches for the modal when pressed.
 */
import { expect, fixture, html } from '@open-wc/testing';
import './history-cutoff-row.ts';
import type { HistoryCutoffRow } from './history-cutoff-row.ts';

describe('history-cutoff-row', () => {
  it('renders nothing when the plan has no window', async () => {
    const el = await fixture<HistoryCutoffRow>(
      html`<history-cutoff-row></history-cutoff-row>`
    );
    expect(el.shadowRoot?.textContent?.trim()).to.equal('');
  });

  it('states the cutoff in days', async () => {
    const el = await fixture<HistoryCutoffRow>(
      html`<history-cutoff-row .days=${90}></history-cutoff-row>`
    );
    expect(el.shadowRoot!.textContent).to.contain(
      'Data older than 90 days is on paid plans'
    );
  });

  it('opens the upgrade modal only when the link is pressed', async () => {
    const seen: CustomEvent[] = [];
    const handler = (event: Event) => seen.push(event as CustomEvent);
    window.addEventListener('show-upgrade-modal', handler);
    const el = await fixture<HistoryCutoffRow>(
      html`<history-cutoff-row .days=${90}></history-cutoff-row>`
    );
    expect(seen).to.have.length(0);
    el.shadowRoot!.querySelector('button')!.click();
    window.removeEventListener('show-upgrade-modal', handler);
    expect(seen).to.have.length(1);
    expect(seen[0].detail.feature).to.equal('analytics_window_days');
  });
});
