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
`;
