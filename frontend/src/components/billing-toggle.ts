import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { segmentedToggleStyles } from './segmented-toggle-styles';

/**
 * Monthly versus Yearly.
 *
 * This is an option INSIDE the Cloud tab, not a peer of the Cloud versus
 * Self-hosted tab bar: it only rewrites the numbers on the cards. `compact`
 * renders it as the small pill that sits above the card row, so the hierarchy
 * is visible without reading the labels.
 */
@customElement('billing-toggle')
export class BillingToggle extends LitElement {
  @property({ type: String, reflect: true })
  interval: 'month' | 'year' = 'month';

  @property({ type: Boolean, reflect: true })
  dark = false;

  /** Small pill form, used above the card row on the Cloud tab. */
  @property({ type: Boolean, reflect: true })
  compact = false;

  private _handleIntervalChange(newInterval: 'month' | 'year') {
    if (this.interval !== newInterval) {
      this.interval = newInterval;
      this.dispatchEvent(
        new CustomEvent('interval-change', {
          detail: { value: this.interval },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  static styles = [
    segmentedToggleStyles,
    css`
      /* The pill form. No vertical margin of its own: the row it sits in owns
         the spacing, and that row keeps its height on the other tab so the
         cards never jump when the visitor switches. */
      .segmented-toggle.compact {
        display: inline-flex;
        justify-content: flex-end;
        margin: 0;
      }

      .segmented-toggle.compact sl-button::part(base) {
        border-radius: 999px;
        font-size: 0.85rem;
        padding: 0 0.35rem;
      }

      .segmented-toggle.compact sl-button:first-of-type::part(base) {
        border-radius: 999px 0 0 999px;
      }

      .segmented-toggle.compact sl-button:last-of-type::part(base) {
        border-radius: 0 999px 999px 0;
      }
    `,
  ];

  render() {
    const size = this.compact ? 'small' : 'medium';
    return html`
      <div
        class="segmented-toggle billing-toggle ${
          this.compact ? 'compact' : ''
        } ${this.dark ? 'dark' : ''}"
      >
        <sl-button-group label="Billing period">
          <sl-button
            size=${size}
            variant=${this.interval === 'month' ? 'primary' : 'default'}
            @click=${() => this._handleIntervalChange('month')}
          >
            Monthly
          </sl-button>
          <sl-button
            size=${size}
            variant=${this.interval === 'year' ? 'primary' : 'default'}
            @click=${() => this._handleIntervalChange('year')}
          >
            Yearly
          </sl-button>
        </sl-button-group>
      </div>
    `;
  }
}
