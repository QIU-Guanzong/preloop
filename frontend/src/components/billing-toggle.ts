import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { segmentedToggleStyles } from './segmented-toggle-styles';

@customElement('billing-toggle')
export class BillingToggle extends LitElement {
  @property({ type: String, reflect: true })
  interval: 'month' | 'year' = 'month';

  @property({ type: Boolean, reflect: true })
  dark = false;

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

  static styles = segmentedToggleStyles;

  render() {
    return html`
      <div class="segmented-toggle billing-toggle ${this.dark ? 'dark' : ''}">
        <sl-button-group label="Billing period">
          <sl-button
            variant=${this.interval === 'month' ? 'primary' : 'default'}
            @click=${() => this._handleIntervalChange('month')}
          >
            Monthly
          </sl-button>
          <sl-button
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
