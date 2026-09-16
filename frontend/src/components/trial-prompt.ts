import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state, query } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import {
  dismissTrialPrompt,
  getTrialPrompt,
  startCheckout,
  type TrialPrompt,
} from '../api';

/**
 * The one-time trial offer, shown on the first console visit after a signup
 * that did not go through checkout.
 *
 * Asked once, and the answer is recorded server-side on the user, so it
 * survives a new browser and a cleared cache. Both answers count as an
 * answer, including picking a plan: someone who opens Stripe and then backs
 * out stays on Free and is not asked again.
 *
 * Server-driven on purpose. Whether there is anything to offer depends on the
 * account's subscription history and on whether this member may buy for the
 * account, neither of which the console can see. The endpoint lives on the
 * billing plugin, so an instance without billing answers 404, `show` is
 * false, and this element renders nothing at all.
 */
@customElement('trial-prompt')
export class TrialPromptElement extends LitElement {
  /**
   * Gate. The console sets this only when the billing feature is on, so an
   * OSS console never even asks the question.
   */
  @property({ type: Boolean }) enabled = false;

  @state() private _prompt: TrialPrompt | null = null;
  @state() private _busy = false;
  @state() private _error = '';

  @query('#trial-dialog') private _dialog?: HTMLElement & {
    show: () => void;
    hide: () => void;
  };

  private _asked = false;

  static styles = css`
    .lead {
      margin: 0 0 1rem 0;
    }

    .plans {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }

    .plan-button::part(base) {
      display: flex;
      justify-content: space-between;
      width: 100%;
    }

    .fineprint {
      margin: 1rem 0 0 0;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-600);
    }
  `;

  updated(changed: Map<string, unknown>) {
    if (changed.has('enabled') && this.enabled && !this._asked) {
      this._asked = true;
      void this._load();
    }
  }

  private async _load() {
    const prompt = await getTrialPrompt();
    if (!prompt.show || !prompt.plans.length) return;
    this._prompt = prompt;
    await this.updateComplete;
    this._dialog?.show();
  }

  /** Record the answer before anything else, so the step is asked once. */
  private async _answer() {
    await dismissTrialPrompt();
  }

  private async _continueFree() {
    this._busy = true;
    try {
      await this._answer();
      this._dialog?.hide();
      this._prompt = null;
    } finally {
      this._busy = false;
    }
  }

  private async _startTrial(planId: string) {
    if (this._busy) return;
    this._busy = true;
    this._error = '';
    try {
      // The answer is recorded first: cancelling at Stripe leaves the account
      // on Free, and re-asking then would be nagging, not onboarding.
      await this._answer();
      const outcome = await startCheckout(planId, 'month', '/console');
      if (outcome && outcome.action !== 'redirect') {
        // Nothing opened (for example the account already has a
        // subscription). Say what the server said and close the step.
        this._error = outcome.message;
      }
    } catch (error) {
      this._error =
        error instanceof Error && error.message
          ? error.message
          : 'Checkout is unavailable right now. You can start a trial later from settings.';
    } finally {
      this._busy = false;
    }
  }

  private _price(monthly: number | null): string {
    if (monthly === null || monthly === undefined) return '';
    return `$${monthly}/mo`;
  }

  render() {
    const prompt = this._prompt;
    if (!prompt) return nothing;
    return html`
      <sl-dialog
        id="trial-dialog"
        label="Start your ${prompt.trial_days}-day trial"
        @sl-request-close=${(event: Event) => {
          // Closing the dialog is an answer too: Free. Let it through, but
          // record it, so the step never comes back unanswered.
          event.preventDefault();
          void this._continueFree();
        }}
      >
        <p class="lead">
          Try a paid plan free for ${prompt.trial_days} days. A card is required
          to start, and nothing is charged until the trial ends. If the trial
          ends without a payment method, the account returns to Free.
        </p>
        <div class="plans">
          ${prompt.plans.map(
            (plan) => html`
              <sl-button
                class="plan-button"
                data-plan=${plan.id}
                variant="primary"
                ?disabled=${this._busy}
                @click=${() => this._startTrial(plan.id)}
              >
                <span>${plan.name}</span>
                <span>${this._price(plan.price_monthly)}</span>
              </sl-button>
            `
          )}
        </div>
        ${this._error ? html`<p role="alert">${this._error}</p>` : nothing}
        <p class="fineprint">
          Annual billing and plan changes live in settings, and you can start a
          trial later from there.
        </p>
        <sl-button
          slot="footer"
          id="continue-free"
          ?disabled=${this._busy}
          @click=${this._continueFree}
        >
          Continue on Free
        </sl-button>
      </sl-dialog>
    `;
  }
}
