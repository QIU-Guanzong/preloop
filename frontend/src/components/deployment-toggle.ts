import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { segmentedToggleStyles } from './segmented-toggle-styles';

/**
 * Cloud versus Self-hosted selector for the public pricing page.
 *
 * This is the TOP-LEVEL axis of the page: it is the only control that changes
 * which cards and which table rows are on screen. `tabs` renders it as a tab
 * bar rather than a small segmented control, so a visitor reads it as the
 * page's primary navigation and the billing period as a secondary option
 * inside the Cloud tab.
 *
 * The labels are properties because the brand names the tabs (EE says
 * "Self-hosted"); the `tab-cloud` / `tab-dedicated` class hooks and the
 * `dedicated` value stay as they are so nothing downstream has to be renamed.
 *
 * Semantics: this is a two-option filter, announced as a group of toggle
 * buttons (`role="group"` + `aria-pressed`), and the tab bar is purely a
 * look. It deliberately does not claim `role="tab"`. The panel it switches
 * lives in the parent view's shadow root, so `aria-controls` cannot reference
 * it (IDREFs do not cross a shadow boundary), and a tab that cannot point at
 * its tabpanel, with no roving tabindex and no arrow keys, describes the
 * control to assistive tech worse than a plain toggle group does.
 */
@customElement('deployment-toggle')
export class DeploymentToggle extends LitElement {
  @property({ type: String, reflect: true })
  deployment: 'cloud' | 'dedicated' = 'cloud';

  @property({ type: Boolean, reflect: true })
  dark = false;

  /** Render as the page's tab bar instead of a small segmented control. */
  @property({ type: Boolean, reflect: true })
  tabs = false;

  @property({ type: String, attribute: 'cloud-label' })
  cloudLabel = 'Cloud';

  @property({ type: String, attribute: 'dedicated-label' })
  dedicatedLabel = 'Self-hosted';

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

  static styles = [
    segmentedToggleStyles,
    css`
      /* The tab bar. Underlined tabs, larger type, and a rule across the full
         width so it reads as the page's primary axis rather than as a second
         toggle sitting next to the billing period. */
      .segmented-toggle.tab-bar {
        display: block;
        margin: 1.75rem 0 0.75rem 0;
      }

      .tab-bar .tab-list {
        display: flex;
        justify-content: center;
        gap: 2.5rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.18);
      }

      .tab-bar button {
        background: none;
        border: none;
        border-radius: 0;
        margin: 0;
        padding: 0.65rem 0.25rem;
        font-size: 1.35rem;
        font-weight: 600;
        line-height: 1.2;
        color: var(--sl-color-neutral-600, #8b949e);
        border-bottom: 3px solid transparent;
      }

      .tab-bar button:hover {
        background: none;
        color: #58a6ff;
        border-color: rgba(88, 166, 255, 0.5);
      }

      .tab-bar button[aria-pressed='true'] {
        background: none;
        color: #58a6ff;
        border-bottom-color: #58a6ff;
      }

      .tab-bar.dark button {
        background-color: transparent;
        border-color: transparent;
        color: #8b949e;
      }

      .tab-bar.dark button:hover,
      .tab-bar.dark button[aria-pressed='true'] {
        background-color: transparent;
        border-bottom-color: #58a6ff;
        color: #58a6ff;
      }

      /* Phone: the bar spans the screen and each tab takes half of it, so the
         two options stay equally weighted instead of huddling in the middle. */
      @media (max-width: 640px) {
        .tab-bar .tab-list {
          display: flex;
          gap: 0;
          width: 100%;
        }

        .tab-bar button {
          flex: 1 1 50%;
          font-size: 1.15rem;
          text-align: center;
        }
      }
    `,
  ];

  render() {
    const variant = this.tabs ? 'tab-bar' : '';
    return html`
      <div
        class="segmented-toggle deployment-toggle ${variant} ${
          this.dark ? 'dark' : ''
        }"
      >
        <div class="tab-list" role="group" aria-label="Deployment">
          <button
            type="button"
            class="tab-cloud"
            aria-pressed=${this.deployment === 'cloud' ? 'true' : 'false'}
            @click=${() => this._select('cloud')}
          >
            ${this.cloudLabel}
          </button>
          <button
            type="button"
            class="tab-dedicated"
            aria-pressed=${this.deployment === 'dedicated' ? 'true' : 'false'}
            @click=${() => this._select('dedicated')}
          >
            ${this.dedicatedLabel}
          </button>
        </div>
      </div>
    `;
  }
}
