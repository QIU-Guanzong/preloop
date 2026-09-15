import type { PricingDeploymentOption } from '../../brand-config';
import { LitElement, html, css, unsafeCSS } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { customElement, state } from 'lit/decorators.js';
import landingStyles from '../../styles/landing.css?inline';
import pricingStyles from '../../styles/pricing-styles.css?inline';
import '../../components/billing-toggle';
import '../../components/deployment-toggle';
import '../../components/pricing-card';

interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: string[];
  badge?: string;
  highlight?: boolean;
  cta_text?: string;
  cta_url?: string;
  description?: string;
  price_label?: string;
  price_note?: string;
  price_note_annual?: string;
  tagline?: string;
  /** Which tab the plan belongs to; anything untagged is a cloud plan. */
  deployment?: 'cloud' | 'dedicated';
}

interface PricingFaq {
  q: string;
  a: string;
}

interface ComparisonRow {
  label: string;
  values: Record<string, string | boolean>;
}

interface ComparisonGroup {
  title: string;
  rows: ComparisonRow[];
}

interface Comparison {
  title?: string;
  note?: string;
  groups: ComparisonGroup[];
}

@customElement('public-pricing-view')
export class PublicPricingView extends LitElement {
  @state() private _interval: 'month' | 'year' = 'year';
  /**
   * Cloud is the default tab: it is what most visitors are buying, and it is
   * the only tab with a price a visitor can act on without talking to us.
   */
  @state() private _deployment: 'cloud' | 'dedicated' = 'cloud';

  @state() private _plans: Plan[] = [];
  @state() private _comparison: Comparison | null = null;
  @state() private _faqs: PricingFaq[] = [];
  @state() private _deployments: PricingDeploymentOption[] = [];
  @state() private _title = 'Pricing';
  @state() private _lead = 'Start free with your own keys.';
  @state() private _billingToggle = true;
  @state() private _loaded = false;

  async connectedCallback() {
    super.connectedCallback();
    await this._loadContent();
  }

  private async _loadContent() {
    try {
      // Prefer slotted SEO content (SSR-injected light DOM) so we keep
      // whatever was actually served to the user / crawler.
      const children = Array.from(this.querySelectorAll('[slot]'));
      const hasSlottedPlan = children.some((el) =>
        el.getAttribute('slot')?.startsWith('plan-')
      );

      if (hasSlottedPlan) {
        this._loadFromSlots(children);
      } else {
        await this._loadFromJson();
      }
    } catch (err) {
      console.error('[pricing-view] Failed to load pricing content:', err);
      // Best-effort fallback: try JSON if slots failed.
      try {
        await this._loadFromJson();
      } catch (err2) {
        console.error('[pricing-view] Failed to load pricing JSON:', err2);
      }
    } finally {
      this._loaded = true;
    }
  }

  private _loadFromSlots(children: Element[]) {
    const heading = children.find(
      (c) => c.getAttribute('slot') === 'pricing-heading'
    );
    if (heading) {
      this._title = heading.getAttribute('data-title') || this._title;
      this._lead = heading.getAttribute('data-lead') || this._lead;
      this._billingToggle =
        heading.getAttribute('data-billing-toggle') !== 'false';
    }
    const plans: Plan[] = [];
    for (let i = 0; i < 20; i++) {
      const el = children.find(
        (c) => c.getAttribute('slot') === `plan-${i}`
      ) as HTMLElement | undefined;
      if (!el) break;
      const featuresAttr = el.getAttribute('data-features') || '';
      const priceMonthlyRaw = el.getAttribute('data-price-monthly');
      const priceAnnuallyRaw = el.getAttribute('data-price-annually');
      const priceMonthly =
        priceMonthlyRaw === '' || priceMonthlyRaw === null
          ? null
          : Number(priceMonthlyRaw);
      const priceAnnually =
        priceAnnuallyRaw === '' || priceAnnuallyRaw === null
          ? null
          : Number(priceAnnuallyRaw);
      plans.push({
        id: el.getAttribute('data-plan-id') || `plan-${i}`,
        name: el.getAttribute('data-plan-name') || '',
        price_monthly: Number.isFinite(priceMonthly)
          ? (priceMonthly as number)
          : null,
        price_annually: Number.isFinite(priceAnnually)
          ? (priceAnnually as number)
          : null,
        price_label: el.getAttribute('data-price-label') || undefined,
        price_note: el.getAttribute('data-price-note') || undefined,
        price_note_annual:
          el.getAttribute('data-price-note-annual') || undefined,
        tagline: el.getAttribute('data-tagline') || undefined,
        badge: el.getAttribute('data-badge') || undefined,
        highlight: el.getAttribute('data-highlight') === 'true',
        cta_text: el.getAttribute('data-cta-text') || undefined,
        cta_url: el.getAttribute('data-cta-url') || undefined,
        deployment:
          el.getAttribute('data-deployment') === 'dedicated'
            ? 'dedicated'
            : 'cloud',
        description: el.getAttribute('data-description') || undefined,
        features: featuresAttr ? featuresAttr.split('|').filter(Boolean) : [],
      });
    }

    // The comparison table is serialised as one JSON blob on its own slot:
    // it is a nested structure, and flattening it into data-* attributes
    // would make the SSR markup unreadable and the parse error-prone.
    const comparisonEl = children.find(
      (c) => c.getAttribute('slot') === 'comparison'
    ) as HTMLElement | undefined;
    if (comparisonEl) {
      const raw = comparisonEl.getAttribute('data-comparison');
      if (raw) {
        try {
          this._comparison = JSON.parse(raw) as Comparison;
        } catch (err) {
          // A malformed table must not take the whole page down: the cards
          // above the fold carry the price, which is the part that matters.
          console.error('[pricing-view] Invalid comparison payload:', err);
        }
      }
    }

    const deployments = children.find(
      (c) => c.getAttribute('slot') === 'deployment-options'
    );
    if (deployments?.getAttribute('data-deployments')) {
      this._deployments = JSON.parse(
        deployments.getAttribute('data-deployments')!
      );
    }

    const faqs: PricingFaq[] = [];
    for (let i = 0; i < 30; i++) {
      const el = children.find((c) => c.getAttribute('slot') === `faq-${i}`) as
        HTMLElement | undefined;
      if (!el) break;
      const q = el.getAttribute('data-q') || '';
      const a = el.getAttribute('data-a') || '';
      if (q && a) faqs.push({ q, a });
    }

    if (plans.length) this._plans = plans;
    if (faqs.length) this._faqs = faqs;
  }

  private async _loadFromJson() {
    const response = await fetch('/landing-content.json');
    if (!response.ok) {
      throw new Error(`Failed to load pricing content: ${response.statusText}`);
    }
    const content = await response.json();
    const pricing = content.pricing || {};
    if (pricing.title) this._title = pricing.title;
    if (pricing.lead) this._lead = pricing.lead;
    if (typeof pricing.billing_toggle === 'boolean') {
      this._billingToggle = pricing.billing_toggle;
    }
    if (Array.isArray(pricing.plans)) {
      this._plans = pricing.plans.map((p: any) => ({
        id: p.id,
        name: p.name,
        price_monthly: p.price_monthly ?? null,
        price_annually: p.price_annually ?? null,
        price_label: p.price_label,
        price_note: p.price_note,
        price_note_annual: p.price_note_annual,
        tagline: p.tagline,
        badge: p.badge,
        highlight: p.highlight,
        cta_text: p.cta_text,
        cta_url: p.cta_url,
        deployment: p.deployment === 'dedicated' ? 'dedicated' : 'cloud',
        description: p.description,
        features: p.features || [],
      }));
    }
    if (pricing.comparison && Array.isArray(pricing.comparison.groups)) {
      this._comparison = pricing.comparison as Comparison;
    }
    if (Array.isArray(pricing.deployment_options))
      this._deployments = pricing.deployment_options;
    if (Array.isArray(pricing.faqs)) {
      this._faqs = pricing.faqs;
    }
  }

  /** Seam for tests: window.location is not stubbable in the runner. */
  private _navigate(url: string) {
    window.location.href = url;
  }

  private _handleSignUpRequest(e: CustomEvent) {
    this._handleSignUp(e.detail.planId);
  }

  private _planById(id: string): Plan | undefined {
    return this._plans.find((p) => p.id === id);
  }

  private async _handleSignUp(planId: string) {
    const plan = this._planById(planId);

    if (planId === 'opensource') {
      const url = plan?.cta_url || 'https://github.com/preloop/preloop';
      if (/^https?:\/\//.test(url)) {
        window.open(url, '_blank');
      } else {
        this._navigate(url);
      }
      return;
    }

    // Enterprise is quoted, never checked out: send it to the contact route.
    if (planId === 'enterprise') {
      this._navigate(plan?.cta_url || '/request-demo');
      return;
    }

    // Existing accounts compare observed usage and confirm a quote in the
    // account view; the public page must never silently switch a subscription.
    if (localStorage.getItem('accessToken')) {
      this._navigate(
        `/console/settings/account?plan=${encodeURIComponent(planId)}&interval=${this._interval}`
      );
      return;
    }
    this._navigate('/register');
  }

  static styles = [
    unsafeCSS(pricingStyles),
    unsafeCSS(landingStyles),
    css`
      /* Two segmented controls side by side on a laptop, stacked on a phone.
         Wrapping is what keeps a narrow screen from squashing either one. */
      .pricing-toggles {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: center;
        column-gap: 1.5rem;
      }

      .deployment-options {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 1.5rem;
      }
      .deployment-options article {
        display: flex;
        flex-direction: column;
        padding: 1.5rem;
        border: 1px solid rgba(230, 237, 243, 0.12);
        border-radius: 20px;
        background-color: #21262f;
      }
      .deployment-options h3 {
        margin-top: 0;
      }
      .deployment-options p {
        line-height: 1.6;
        flex: 1 1 auto;
      }
      .deployment-options a {
        display: inline-block;
        margin-top: auto;
        padding: 0.75rem 0;
        font-weight: 600;
        color: #58a6ff;
      }
      .loading,
      .error {
        text-align: center;
        margin: 2rem 0;
      }
      .error {
        color: var(--sl-color-danger-600);
      }

      /* Four cards need a tighter minimum than the shared 260px grid or the
         ladder wraps to two rows on ordinary laptop widths. */
      .plans-grid {
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      }

      .comparison-section {
        margin-top: 3.5rem;
      }

      /* Narrow screens scroll the table sideways instead of squashing four
         columns into unreadable slivers. */
      .comparison-scroll {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }

      .comparison-table {
        width: 100%;
        min-width: 720px;
        margin: 0 auto;
        border-collapse: collapse;
        background-color: #21262f;
        color: #e6edf3;
        border-radius: 16px;
        overflow: hidden;
      }

      .comparison-table th,
      .comparison-table td {
        font-size: 0.95rem;
        padding: 0.75rem 1rem;
        text-align: center;
        vertical-align: middle;
        border-bottom: 1px solid rgba(230, 237, 243, 0.12);
      }

      .comparison-table thead th {
        font-size: 1.05rem;
        font-weight: 600;
        border-bottom: 2px solid #58a6ff;
      }

      .comparison-table .row-label {
        text-align: left;
        font-weight: 500;
        min-width: 220px;
      }

      .comparison-table .group-row th {
        text-align: left;
        font-size: 0.8rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: #8b949e;
        padding-top: 1.5rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.2);
      }

      .comparison-table tbody tr:last-child th,
      .comparison-table tbody tr:last-child td {
        border-bottom: none;
      }

      .check-mark sl-icon {
        color: #58a6ff;
        font-size: 1.2rem;
      }

      .cross-mark sl-icon {
        color: #6e7681;
        font-size: 1.1rem;
      }

      .comparison-note {
        margin-top: 1rem;
        text-align: center;
        font-size: 0.9rem;
        color: var(--sl-color-text-secondary);
      }

      .hero-content .lead {
        margin: 0 auto;
        text-align: center;
      }
    `,
  ];

  /**
   * A brand with no dedicated plan and no deployment options gets no tab
   * selector at all, so a cloud-only page still renders exactly as before.
   */
  private _hasDedicated(): boolean {
    return this._dedicatedOptions().length > 0;
  }

  /** Hosted subscriptions: the cards and the comparison table. */
  private _cloudPlans(): Plan[] {
    return this._plans.filter((p) => p.deployment !== 'dedicated');
  }

  /**
   * The Dedicated tab: the configured deployment options, then any plan the
   * catalog marked as dedicated (Enterprise). Both are quoted rather than
   * bought, so they render as the same card shape and link straight to the
   * contact or source route instead of carrying a price toggle.
   */
  private _dedicatedOptions(): PricingDeploymentOption[] {
    const quotedPlans = this._plans
      .filter((p) => p.deployment === 'dedicated')
      .map((plan) => ({
        title: plan.price_label
          ? `${plan.name}: ${plan.price_label}`
          : plan.name,
        description: plan.tagline || plan.description || '',
        cta_text: plan.cta_text || 'Contact us',
        cta_url: plan.cta_url || '/request-demo',
      }));
    return [...this._deployments, ...quotedPlans];
  }

  private _renderCards() {
    return html`
      <div class="plans-grid" @signup-requested=${this._handleSignUpRequest}>
        ${this._cloudPlans().map(
          (plan) => html`
            <pricing-card
              .plan=${plan}
              .interval=${this._interval}
              .dark=${true}
            ></pricing-card>
          `
        )}
      </div>
    `;
  }

  /** Render one comparison cell: booleans become marks, text prints as-is. */
  private _renderCell(value: string | boolean | undefined) {
    if (value === true) {
      return html`<span class="check-mark"
        ><sl-icon name="check-lg" label="Included"></sl-icon
      ></span>`;
    }
    if (value === false) {
      return html`<span class="cross-mark"
        ><sl-icon name="x-lg" label="Not included"></sl-icon
      ></span>`;
    }
    // Undefined renders empty rather than as a dash or a "no": an unstated
    // limit is not a claim that the plan lacks the capability.
    return value ?? '';
  }

  /**
   * The below-the-fold comparison table. Quotas, retention, and the feature
   * split live here, keeping every card to one number plus one line.
   */
  private _renderComparison() {
    const plans = this._cloudPlans();
    if (!this._comparison?.groups?.length || !plans.length) return '';

    const planIds = plans.map((p) => p.id);
    const colCount = planIds.length + 1;

    return html`
      <section class="comparison-section">
        <div class="section-container">
          <h2 class="text-center">
            ${this._comparison.title || 'Compare plans'}
          </h2>
          <div class="comparison-scroll">
            <table class="comparison-table">
              <thead>
                <tr>
                  <th scope="col" class="row-label"></th>
                  ${plans.map(
                    (plan) => html`<th scope="col">${plan.name}</th>`
                  )}
                </tr>
              </thead>
              <tbody>
                ${this._comparison.groups.map(
                  (group) => html`
                    <tr class="group-row">
                      <th scope="colgroup" colspan=${colCount}>
                        ${group.title}
                      </th>
                    </tr>
                    ${group.rows.map(
                      (row) => html`
                        <tr>
                          <th scope="row" class="row-label">${row.label}</th>
                          ${planIds.map(
                            (id) =>
                              html`<td>
                                ${this._renderCell(row.values?.[id])}
                              </td>`
                          )}
                        </tr>
                      `
                    )}
                  `
                )}
              </tbody>
            </table>
          </div>
          ${
            this._comparison.note
              ? html`<p class="comparison-note">${this._comparison.note}</p>`
              : ''
          }
        </div>
      </section>
    `;
  }

  private _handleFaqClick(e: Event) {
    e.preventDefault();
    const summary = e.currentTarget as HTMLElement;
    const details = summary.parentElement as HTMLDetailsElement;
    const answer = summary.nextElementSibling as HTMLElement | null;
    if (!answer) return;

    if (details.open) {
      answer.style.height = `${answer.scrollHeight}px`;
      requestAnimationFrame(() => {
        answer.style.height = '0px';
      });
      answer.addEventListener(
        'transitionend',
        () => {
          details.removeAttribute('open');
        },
        { once: true }
      );
    } else {
      details.setAttribute('open', '');
      answer.style.height = `${answer.scrollHeight}px`;
      answer.addEventListener(
        'transitionend',
        () => {
          if (details.open) {
            answer.style.height = 'auto';
          }
        },
        { once: true }
      );
    }
  }

  private _renderDedicated() {
    const options = this._dedicatedOptions();
    if (!options.length) return '';
    return html`<div
      class="deployment-options"
      aria-label="Dedicated and self-hosted options"
    >
      ${options.map((option) => {
        const external = /^https?:\/\//.test(option.cta_url);
        return html`<article>
          <h3>${option.title}</h3>
          <p>${option.description}</p>
          <a
            href=${option.cta_url}
            target=${external ? '_blank' : '_self'}
            rel=${external ? 'noopener noreferrer' : ''}
            >${option.cta_text}</a
          >
        </article>`;
      })}
    </div>`;
  }

  private _renderFaqs() {
    if (!this._faqs.length) return '';
    return html`
      <section class="pricing-faq main-section faq-section">
        <div class="section-container">
          <h2 class="text-center">Frequently Asked Questions</h2>
          <div class="faq-list">
            ${this._faqs.map(
              (faq) => html`
                <details class="faq-item">
                  <summary class="faq-question" @click=${this._handleFaqClick}>
                    <span>${faq.q}</span>
                    <sl-icon name="chevron-down"></sl-icon>
                  </summary>
                  <div class="faq-answer">
                    <div class="faq-answer-content">${unsafeHTML(faq.a)}</div>
                  </div>
                </details>
              `
            )}
          </div>
        </div>
      </section>
    `;
  }

  render() {
    return html`
      <app-header></app-header>
      <main>
        <section class="main-section">
          <div class="section-container hero-inner">
            <div class="hero-content">
              <h1 class="fw-bold">
                <span class="gradient-product">${this._title}</span>
              </h1>
              <p class="lead">${this._lead}</p>
            </div>
          </div>

          <div class="section-container">
            <div class="pricing-toggles">
              ${
                // The tab choice governs everything below it, so it leads.
                // Keeping it leftmost also means the row does not reflow when
                // the period toggle disappears on the Dedicated tab.
                this._hasDedicated()
                  ? html`<deployment-toggle
                      .dark=${true}
                      .deployment=${this._deployment}
                      @deployment-change=${(e: CustomEvent) =>
                        (this._deployment = e.detail.value)}
                    ></deployment-toggle>`
                  : ''
              }
              ${
                // The billing period only exists for hosted subscriptions:
                // nothing on the Dedicated tab is priced per month, so the
                // toggle is removed there rather than left inert.
                this._billingToggle && this._deployment === 'cloud'
                  ? html`<billing-toggle
                      .dark=${true}
                      .interval=${this._interval}
                      @interval-change=${(e: CustomEvent) =>
                        (this._interval = e.detail.value)}
                    ></billing-toggle>`
                  : ''
              }
            </div>
            ${
              this._deployment === 'dedicated'
                ? this._renderDedicated()
                : this._cloudPlans().length
                  ? this._renderCards()
                  : ''
            }
          </div>
        </section>
        ${this._deployment === 'cloud' ? this._renderComparison() : ''}
        ${this._renderFaqs()}
      </main>
      <app-footer></app-footer>
    `;
  }
}
