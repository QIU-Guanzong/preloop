import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getUsageNudges, getUserProfile, NO_USAGE_NUDGES } from '../api';
import type { UsageNudge } from '../api';
import {
  bandFor,
  dismissNudge,
  loadNudgeDismissals,
  nudgeLadder,
  nudgeLink,
  nudgeMessage,
  visibleNudges,
  NUDGE_BANDS,
  NUDGE_THRESHOLD,
  type NudgeDismissals,
} from '../utils/usage-nudges';

/**
 * What the account is close to running out of, stated once.
 *
 * This replaces the upgrade prompt that used to open on first load. A modal
 * that arrives before the person has done anything is a toll gate on a
 * product they are still evaluating; the paywall modal now belongs to the
 * action that actually hit the limit (a price override, a hosted-model
 * optimization, asking for data older than the plan's analytics window).
 * What is left here is information: one line per limit the account is at or
 * past half of, with the number in it, and a link to the plan page with the
 * limit named.
 *
 * It is not attention (DESIGN.md "Attention"): nothing here is broken and
 * nobody has to act today, so it uses the neutral surface and an info mark
 * rather than amber. Amber is reserved for things that need a person.
 *
 * Failure is silence. OSS has no billing plugin and no such endpoint, so
 * `getUsageNudges()` answers with an empty list on 404 and this component
 * renders nothing at all - the OSS console is byte for byte the console it
 * was before this banner existed.
 */
@customElement('usage-nudge-banner')
export class UsageNudgeBanner extends LitElement {
  @state()
  private nudges: UsageNudge[] = [];

  /** The ladder the server nudges on, or this build's until it answers. */
  @state()
  private bands: readonly number[] = NUDGE_BANDS;

  /** Where nudging starts, for a dismissal recorded below the first band. */
  @state()
  private threshold = NUDGE_THRESHOLD;

  @state()
  private dismissals: NudgeDismissals = {};

  /** Whose dismissals these are. Empty until the profile lands. */
  @state()
  private userId = '';

  private readonly onAuthChange = () => {
    void this.refresh();
  };

  static styles = css`
    :host {
      display: block;
    }

    .banner {
      display: flex;
      flex-direction: column;
      gap: 6px;
      padding: 10px var(--console-page-padding-x, 24px);
      background: var(--console-surface);
      border-bottom: 1px solid var(--console-hairline);
      color: var(--console-body-color);
      font-size: var(--console-text-body, 14px);
      line-height: 1.4;
    }

    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .icon {
      flex-shrink: 0;
      font-size: 14px;
      color: var(--console-meta-color);
      line-height: 1;
    }

    .message {
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }

    a.plans {
      flex-shrink: 0;
      color: var(--console-link-color);
      font-size: var(--console-text-meta, 13px);
      font-weight: 600;
      text-decoration: none;
    }

    a.plans:hover {
      text-decoration: underline;
    }

    button.dismiss {
      flex-shrink: 0;
      background: none;
      border: none;
      padding: 2px 6px;
      border-radius: var(--sl-border-radius-small, 3px);
      color: var(--console-meta-color);
      font-size: 15px;
      line-height: 1;
      cursor: pointer;
    }

    button.dismiss:hover {
      background: var(--console-hover-tint);
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    this.dismissals = {};
    void this.refresh();
    // Sign-in is the other moment this check runs: the shell can already be
    // mounted when a second account signs in on the same tab.
    window.addEventListener('auth-change', this.onAuthChange);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('auth-change', this.onAuthChange);
  }

  /**
   * Reload usage and the caller's identity.
   *
   * Both calls are cheap (the server caches the nudge list for a minute and
   * the profile is cached client-side), both fail into "no banner", and
   * neither can open a modal: this is a background fetch, and the paywall
   * modal only ever follows a user action.
   */
  private async refresh() {
    const [profile, payload] = await Promise.all([
      getUserProfile().catch(() => null),
      getUsageNudges().catch(() => NO_USAGE_NUDGES),
    ]);
    this.userId = profile?.id ?? '';
    this.dismissals = this.userId ? loadNudgeDismissals(this.userId) : {};
    const ladder = nudgeLadder(payload);
    this.threshold = ladder.threshold;
    this.bands = ladder.bands;
    this.nudges = payload.nudges;
  }

  private handleDismiss(nudge: UsageNudge) {
    // Store the band it stood at, not "hidden": the same limit at 80 percent
    // is news again, the same limit at 51 is not.
    const band = bandFor(nudge.ratio, this.bands) ?? this.threshold;
    this.dismissals = dismissNudge(this.userId, nudge.key, band);
  }

  render() {
    const visible = visibleNudges(this.nudges, this.dismissals, this.bands);
    if (visible.length === 0) return nothing;

    return html`
      <div class="banner" role="status">
        ${visible.map(
          (nudge) => html`
            <div class="row" data-nudge=${nudge.key}>
              <span class="icon" aria-hidden="true">&#9432;</span>
              <span class="message">${nudgeMessage(nudge)}</span>
              <a class="plans" href=${nudgeLink(nudge.key)}>See plans</a>
              <button
                class="dismiss"
                aria-label="Dismiss"
                title="Dismiss"
                @click=${() => this.handleDismiss(nudge)}
              >
                &times;
              </button>
            </div>
          `
        )}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'usage-nudge-banner': UsageNudgeBanner;
  }
}
