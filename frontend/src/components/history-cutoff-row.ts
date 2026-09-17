import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import {
  historyCutoffMessage,
  requestHistoryUpgrade,
} from '../utils/history-window';

/**
 * The line where a plan's analytics window ends.
 *
 * A list that simply stops at 90 days reads as "nothing happened before
 * then", which is the one thing it does not mean. So the list ends with a
 * row that says what the gap is, in the same row rhythm as the data above
 * it, and offers the plan that lifts it.
 *
 * Pressing the link is a user action, so it may open the upgrade modal.
 * The row itself never does: it is a label, not a prompt.
 *
 * `days` null means there is no window to state (OSS, or an unlimited plan),
 * and then there is no row at all. With no plan left to move to, the row
 * states the window and offers nothing: a "See plans" control that leads
 * only back to the plan you are on is a dead end with a price tag on it.
 *
 * "A plan left to move to" is the plan id the server sent, not its display
 * name. The name is only how the sentence reads: a payload that names a plan
 * the console cannot put a word to (an older server, a catalog the console
 * has never seen) still has somewhere to send the reader, so the offer
 * stands and the sentence simply stops naming a plan. The window itself is
 * whatever the server said, verbatim.
 */
@customElement('history-cutoff-row')
export class HistoryCutoffRow extends LitElement {
  /** The plan's analytics window in days, or null when there is none. */
  @property({ type: Number })
  days: number | null = null;

  /** Name of the cheapest plan with a longer window, or null at the top. */
  @property({ type: String, attribute: 'unlocks-at-plan-name' })
  unlocksAtPlanName: string | null = null;

  /** Id of that plan. Present without a name on a plan the catalog renamed. */
  @property({ type: String, attribute: 'unlocks-at-plan' })
  unlocksAtPlan: string | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 0;
      border-top: 1px solid var(--console-hairline);
      color: var(--console-meta-color);
      font-size: var(--console-text-meta, 13px);
      line-height: 1.4;
    }

    .message {
      flex: 1;
      min-width: 0;
    }

    button {
      flex-shrink: 0;
      background: none;
      border: none;
      padding: 0;
      color: var(--console-link-color);
      font-size: var(--console-text-meta, 13px);
      font-weight: 600;
      cursor: pointer;
    }

    button:hover {
      text-decoration: underline;
    }
  `;

  private handleSeePlans() {
    requestHistoryUpgrade();
  }

  render() {
    if (this.days === null || !Number.isFinite(this.days) || this.days <= 0) {
      return nothing;
    }
    return html`
      <div class="row" data-testid="history-cutoff-row">
        <span class="message"
          >${historyCutoffMessage(this.days, this.unlocksAtPlanName)}</span
        >
        ${
          this.unlocksAtPlanName || this.unlocksAtPlan
            ? html`<button type="button" @click=${this.handleSeePlans}>
                See plans
              </button>`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'history-cutoff-row': HistoryCutoffRow;
  }
}
