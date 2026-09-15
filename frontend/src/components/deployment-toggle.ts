import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { segmentedToggleStyles } from './segmented-toggle-styles';

/**
 * Cloud versus Dedicated selector for the public pricing page.
 *
 * Cloud is the hosted subscription ladder with a billing period and a
 * comparison table; Dedicated covers the self-managed and quoted options,
 * which have no monthly price and so no period to choose. Keeping the two
 * groups on separate tabs is what stops the page from presenting five
 * unrelated columns at once.
 */
@customElement('deployment-toggle')
export class DeploymentToggle extends LitElement {
  @property({ type: String, reflect: true })
  deployment: 'cloud' | 'dedicated' = 'cloud';

  @property({ type: Boolean, reflect: true })
  dark = false;

  private _select(value: 'cloud' | 'dedicated') {
    if (this.deployment === value) return;
    this.deployment = value;
    this.dispatchEvent(
      new CustomEvent('deployment-change', {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
  }

  static styles = segmentedToggleStyles;

  render() {
    return html`
      <div
        class="segmented-toggle deployment-toggle ${this.dark ? 'dark' : ''}"
      >
        <sl-button-group label="Deployment">
          <sl-button
            class="tab-cloud"
            aria-pressed=${this.deployment === 'cloud' ? 'true' : 'false'}
            variant=${this.deployment === 'cloud' ? 'primary' : 'default'}
            @click=${() => this._select('cloud')}
          >
            Cloud
          </sl-button>
          <sl-button
            class="tab-dedicated"
            aria-pressed=${this.deployment === 'dedicated' ? 'true' : 'false'}
            variant=${this.deployment === 'dedicated' ? 'primary' : 'default'}
            @click=${() => this._select('dedicated')}
          >
            Dedicated
          </sl-button>
        </sl-button-group>
      </div>
    `;
  }
}
