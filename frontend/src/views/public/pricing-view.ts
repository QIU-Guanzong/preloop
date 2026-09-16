import { LitElement, html, css, unsafeCSS } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { customElement, state } from 'lit/decorators.js';
import landingStyles from '../../styles/landing.css?inline';
import pricingStyles from '../../styles/pricing-styles.css?inline';
import '../../components/billing-toggle';
import '../../components/deployment-toggle';
import '../../components/pricing-card';
import {
  CLOUD_COMPARISON_FALLBACK_TITLE,
  CLOUD_LEAD_FALLBACK,
  CLOUD_TAB_FALLBACK_LABEL,
  DEDICATED_COMPARISON_FALLBACK_TITLE,
  DEDICATED_TAB_FALLBACK_LABEL,
} from '../../pricing-ssr';

interface Plan {
  id: string;
  name: string;
  /** Small line under the name; only the plans that declare one get it. */
  subtitle?: string;
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

/**
 * The public pricing page.
 *
 * Cloud versus Self-hosted is the TOP-LEVEL axis and it is the only thing that
 * changes which cards and which table rows are on screen, so it renders as a
 * tab bar directly under the page heading with its own one-line lead.
 *
 * Monthly versus Yearly is an option INSIDE the Cloud tab: a small pill above
 * the card row that rewrites nothing but the printed numbers. It does not
 * exist on Self-hosted, where nothing is priced per month, but its row keeps
 * its height there so the cards do not jump when the visitor switches tabs.
 *
 * Both tabs render through the same two methods (`_renderCards` and
 * `_renderComparison`), so the layout is identical and only the columns
 * differ.
 */
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
  /** The Self-hosted tab's cards; editions, not subscriptions. */
  @state() private _dedicatedPlans: Plan[] = [];
  @state() private _dedicatedComparison: Comparison | null = null;
  @state() private _faqs: PricingFaq[] = [];
  @state() private _title = 'Pricing';
  @state() private _lead = 'Start free with your own keys.';
  /** Tab labels and the per-tab leads. The brand names both tabs. */
  @state() private _cloudLabel = CLOUD_TAB_FALLBACK_LABEL;
  @state() private _dedicatedLabel = DEDICATED_TAB_FALLBACK_LABEL;
  @state() private _cloudLead = CLOUD_LEAD_FALLBACK;
  @state() private _dedicatedLead = '';
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
      const hasSlottedPlan = children.some((el) => {
        const slot = el.getAttribute('slot');
        return slot?.startsWith('plan-') || slot?.startsWith('dedicated-plan-');
      });

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
      this._cloudLabel =
        heading.getAttribute('data-cloud-label') || this._cloudLabel;
      this._dedicatedLabel =
        heading.getAttribute('data-dedicated-label') || this._dedicatedLabel;
      this._cloudLead =
        heading.getAttribute('data-cloud-lead') || this._cloudLead;
      this._dedicatedLead =
        heading.getAttribute('data-dedicated-lead') || this._dedicatedLead;
      this._billingToggle =
        heading.getAttribute('data-billing-toggle') !== 'false';
    }
    const plans = this._plansFromSlots(children, 'plan');
    // The Self-hosted tab is server-rendered as its own set of slots so the
    // crawler sees both tabs' cards. Falling back to the tagged plans keeps a
    // brand that only marks a plan `dedicated` working unchanged.
    const dedicated = this._plansFromSlots(children, 'dedicated-plan');

    this._comparison =
      this._comparisonFromSlot(children, 'comparison') ?? this._comparison;
    this._dedicatedComparison =
      this._comparisonFromSlot(children, 'dedicated-comparison') ??
      this._dedicatedComparison;

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
    if (dedicated.length) this._dedicatedPlans = dedicated;
    if (faqs.length) this._faqs = faqs;
  }

  /** Read one numbered run of `slot="<prefix>-N"` cards out of the SSR markup. */
  private _plansFromSlots(children: Element[], prefix: string): Plan[] {
    const plans: Plan[] = [];
    for (let i = 0; i < 20; i++) {
      const el = children.find(
        (c) => c.getAttribute('slot') === `${prefix}-${i}`
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
        subtitle: el.getAttribute('data-subtitle') || undefined,
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
    return plans;
  }

  /**
   * A comparison table is serialised as one JSON blob on its own slot: it is
   * a nested structure, and flattening it into data-* attributes would make
   * the SSR markup unreadable and the parse error-prone. Returns null when
   * the slot is absent or malformed, so a broken table never takes the cards
   * with it.
   */
  private _comparisonFromSlot(
    children: Element[],
    slot: string
  ): Comparison | null {
    const el = children.find((c) => c.getAttribute('slot') === slot) as
      HTMLElement | undefined;
    const raw = el?.getAttribute('data-comparison');
    if (!raw) return null;
    try {
      return JSON.parse(raw) as Comparison;
    } catch (err) {
      console.error('[pricing-view] Invalid comparison payload:', err);
      return null;
    }
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
    if (pricing.cloud_label) this._cloudLabel = pricing.cloud_label;
    if (pricing.cloud_lead) this._cloudLead = pricing.cloud_lead;
    if (pricing.dedicated?.label)
      this._dedicatedLabel = pricing.dedicated.label;
    if (pricing.dedicated?.lead) this._dedicatedLead = pricing.dedicated.lead;
    if (typeof pricing.billing_toggle === 'boolean') {
      this._billingToggle = pricing.billing_toggle;
    }
    const toPlan = (p: any, fallback?: 'cloud' | 'dedicated'): Plan => ({
      id: p.id,
      name: p.name,
      subtitle: p.subtitle,
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
      deployment:
        (p.deployment ?? fallback) === 'dedicated' ? 'dedicated' : 'cloud',
      description: p.description,
      features: p.features || [],
    });
    if (Array.isArray(pricing.plans)) {
      this._plans = pricing.plans.map((p: any) => toPlan(p));
    }
    if (pricing.comparison && Array.isArray(pricing.comparison.groups)) {
      this._comparison = pricing.comparison as Comparison;
    }
    // The brand's own `dedicated` block wins; otherwise a plan tagged
    // `dedicated` in the catalog still lands on the second tab.
    if (Array.isArray(pricing.dedicated?.plans)) {
      this._dedicatedPlans = pricing.dedicated.plans.map((p: any) =>
        toPlan(p, 'dedicated')
      );
    }
    if (Array.isArray(pricing.dedicated?.comparison?.groups)) {
      this._dedicatedComparison = pricing.dedicated.comparison as Comparison;
    }
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
    return (
      this._plans.find((p) => p.id === id) ??
      this._dedicatedPlans.find((p) => p.id === id)
    );
  }

  private _followLink(url: string) {
    if (/^https?:\/\//.test(url)) {
      window.open(url, '_blank', 'noopener,noreferrer');
    } else {
      this._navigate(url);
    }
  }

  private async _handleSignUp(planId: string) {
    const plan = this._planById(planId);

    // Nothing on the Self-hosted tab is a subscription a visitor can buy: every
    // edition there is quoted or downloaded, so its CTA is the only route.
    // Shape, not plan id: opensource and enterprise are dedicated in every
    // path that reaches here, so a per-id branch would disagree with this one
    // (an external enterprise cta_url must open a new tab, same as the rest).
    if (plan?.deployment === 'dedicated') {
      this._followLink(plan.cta_url || '/request-demo');
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
      /* The top-level axis. Full width so the tab bar's rule spans the card
         row underneath it and the two tabs read as the page's navigation. */
      .pricing-tabs {
        display: block;
      }

      /* One line per tab, directly under the bar. Centred and quiet: it
         explains the tab, it does not compete with the heading. The row keeps
         one line of height even when a brand leaves a tab's lead empty, for
         the same reason .period-row does: switching tabs must not move the
         card row. */
      .tab-lead {
        margin: 0.75rem auto 0 auto;
        text-align: center;
        font-size: 1rem;
        line-height: 1.5;
        min-height: 1.5rem;
        color: var(--sl-color-text-secondary);
        max-width: 46rem;
      }

      /* The period pill's row. Right-aligned above the cards on a laptop and
         ALWAYS this tall, including on Self-hosted where the pill is not
         rendered, so switching tabs never moves the card row. */
      .period-row {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        min-height: 3rem;
        margin: 1.25rem 0 0.5rem 0;
      }

      /* Phone: the pill is centred over the cards rather than pinned to an
         edge the eye has no reason to look at. */
      @media (max-width: 640px) {
        .period-row {
          justify-content: center;
        }
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
   * A brand with no self-hosted edition gets no tab bar at all, so a
   * cloud-only page still renders exactly as before.
   */
  private _hasDedicated(): boolean {
    return this._dedicatedCards().length > 0;
  }

  /**
   * Resolve the visible tab in one place. Cloud is the default when it has
   * cards; if every configured plan is dedicated, fall back so the page is
   * not an empty container.
   */
  private _activeDeployment(): 'cloud' | 'dedicated' {
    return this._cloudPlans().length ? this._deployment : 'dedicated';
  }

  /** Hosted subscriptions: the cards and the comparison table. */
  private _cloudPlans(): Plan[] {
    return this._plans.filter((p) => p.deployment !== 'dedicated');
  }

  /**
   * The Self-hosted tab's cards. The brand's own `dedicated` block wins; a plan
   * the catalog tagged `dedicated` is the fallback, so a brand that never
   * configures editions still gets a second tab rather than nothing.
   */
  private _dedicatedCards(): Plan[] {
    if (this._dedicatedPlans.length) return this._dedicatedPlans;
    return this._plans.filter((p) => p.deployment === 'dedicated');
  }

  /** The one-line lead for whichever tab is on screen. */
  private _activeLead(): string {
    return this._activeDeployment() === 'cloud'
      ? this._cloudLead
      : this._dedicatedLead;
  }

  /** The cards for whichever tab is on screen. */
  private _activeCards(): Plan[] {
    return this._activeDeployment() === 'cloud'
      ? this._cloudPlans()
      : this._dedicatedCards();
  }

  /** The table for whichever tab is on screen. */
  private _activeComparison(): Comparison | null {
    return this._activeDeployment() === 'cloud'
      ? this._comparison
      : this._dedicatedComparison;
  }

  private _renderCards(plans: Plan[]) {
    if (!plans.length) return '';
    return html`
      <div class="plans-grid" @signup-requested=${this._handleSignUpRequest}>
        ${plans.map(
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
   *
   * Both tabs use this method: Cloud compares Free/Pro/Team/Business, and
   * Self-hosted compares the editions. Only the columns and the rows differ,
   * never the shape of the table.
   */
  private _renderComparison(
    comparison: Comparison | null,
    plans: Plan[],
    fallbackTitle: string
  ) {
    if (!comparison?.groups?.length || !plans.length) return '';

    const planIds = plans.map((p) => p.id);
    const colCount = planIds.length + 1;

    return html`
      <section class="comparison-section">
        <div class="section-container">
          <h2 class="text-center">${comparison.title || fallbackTitle}</h2>
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
                ${comparison.groups.map(
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
            comparison.note
              ? html`<p class="comparison-note">${comparison.note}</p>`
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
    const deployment = this._activeDeployment();
    const plans = this._activeCards();
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
            ${
              // The top-level axis: it governs every card and every table row
              // below it, so it renders as a tab bar under the heading rather
              // than as one of two equal-looking controls in a row.
              this._hasDedicated()
                ? html`<div class="pricing-tabs">
                    <deployment-toggle
                      .dark=${true}
                      .tabs=${true}
                      .cloudLabel=${this._cloudLabel}
                      .dedicatedLabel=${this._dedicatedLabel}
                      .deployment=${deployment}
                      @deployment-change=${(e: CustomEvent) =>
                        (this._deployment = e.detail.value)}
                    ></deployment-toggle>
                  </div>`
                : ''
            }
            ${
              // One line per tab. Each tab is a different product, so each
              // gets its own sentence instead of a single lead that has to
              // hedge across both. On a two-tab page the element is rendered
              // even when a brand leaves one lead empty ("dedicated.lead" is
              // optional), because a vanishing line would move the card row
              // by two lines, which is the jump .period-row already prevents
              // one level down.
              this._hasDedicated() || this._activeLead()
                ? html`<p class="tab-lead">${this._activeLead()}</p>`
                : ''
            }
            <div class="period-row">
              ${
                // The billing period only exists for hosted subscriptions: an
                // open-source edition is free and a quoted one is agreed per
                // year, so on Self-hosted the pill is not rendered. The row
                // keeps its height either way, so the cards stay put.
                this._billingToggle && deployment === 'cloud'
                  ? html`<billing-toggle
                      .dark=${true}
                      .compact=${true}
                      .interval=${this._interval}
                      @interval-change=${(e: CustomEvent) =>
                        (this._interval = e.detail.value)}
                    ></billing-toggle>`
                  : ''
              }
            </div>
            ${this._renderCards(plans)}
          </div>
        </section>
        ${this._renderComparison(
          this._activeComparison(),
          plans,
          deployment === 'cloud'
            ? CLOUD_COMPARISON_FALLBACK_TITLE
            : DEDICATED_COMPARISON_FALLBACK_TITLE
        )}
        ${this._renderFaqs()}
      </main>
      <app-footer></app-footer>
    `;
  }
}
