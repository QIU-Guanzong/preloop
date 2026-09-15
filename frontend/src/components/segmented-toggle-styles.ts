import { css } from 'lit';

/**
 * The shared look of the pricing page's segmented toggles.
 *
 * The page carries two of them (billing period, and cloud versus dedicated
 * deployment). They are separate components because they hold different state
 * and emit different events, but a visitor must read them as one control
 * style, so the styles live here rather than being copied per component.
 */
export const segmentedToggleStyles = css`
  .segmented-toggle {
    display: flex;
    justify-content: center;
    margin: 1.5rem 0 2rem 0;
  }

  .segmented-toggle.dark sl-button[variant='default']::part(base) {
    background-color: transparent;
    border-color: #58a6ff;
    color: #58a6ff;
  }

  .segmented-toggle.dark sl-button[variant='default']::part(base):hover {
    background-color: #58a6ff;
    color: white;
  }

  .segmented-toggle.dark sl-button[variant='primary']::part(base) {
    background-color: #58a6ff;
    border-color: #58a6ff;
    color: white;
  }

  sl-button-group {
    position: relative;
    --sl-button-group-spacing: 0;
  }

  /* Native buttons so aria-pressed reaches the accessibility tree.
     sl-button does not forward aria-* onto the real <button> in its
     shadow root. Keep the same connected pair and #58a6ff pairing
     already used by the Shoelace path on this page. */
  .segmented-toggle .tab-list {
    display: inline-flex;
  }

  .segmented-toggle button {
    appearance: none;
    font: inherit;
    font-weight: 500;
    line-height: 1.2;
    padding: 0.5rem 1.25rem;
    margin: 0;
    cursor: pointer;
    background: var(--sl-color-neutral-0, #fff);
    color: var(--sl-color-neutral-700, #3d3d3d);
    border: 1px solid var(--sl-color-neutral-300, #d4d4d8);
  }

  .segmented-toggle button:first-child {
    border-radius: 4px 0 0 4px;
  }

  .segmented-toggle button:last-child {
    border-radius: 0 4px 4px 0;
    margin-left: -1px;
  }

  .segmented-toggle button[aria-pressed='true'] {
    background: var(--sl-color-primary-600, #0284c7);
    border-color: var(--sl-color-primary-600, #0284c7);
    color: #fff;
    z-index: 1;
  }

  .segmented-toggle button:hover {
    background: var(--sl-color-primary-600, #0284c7);
    border-color: var(--sl-color-primary-600, #0284c7);
    color: #fff;
  }

  .segmented-toggle button:focus-visible {
    outline: 2px solid #58a6ff;
    outline-offset: 2px;
    z-index: 1;
  }

  .segmented-toggle.dark button {
    background-color: transparent;
    border-color: #58a6ff;
    color: #58a6ff;
  }

  .segmented-toggle.dark button:hover,
  .segmented-toggle.dark button[aria-pressed='true'] {
    background-color: #58a6ff;
    border-color: #58a6ff;
    color: #fff;
  }
`;
