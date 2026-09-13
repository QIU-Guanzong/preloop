import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fetchWithAuth } from '../api';
import type {
  BillingMonth,
  BillingNotice,
  BillingPlan,
  PlanAssessment,
  PlanChangeOptions,
  PlanChangePreview,
  PlanChangeResult,
} from '../types/billing';

const CAPABILITIES: Record<string, string> = {
  ai_optimization: 'Built-in model optimization',
  value_reviews: 'Value reviews',
  rbac: 'Role-based access control',
  team_approvals: 'Team approval workflows',
  price_overrides: 'Model price overrides',
  reconciliation: 'Provider billing reconciliation',
};
const COVERAGE_REASONS: Record<string, string> = {
  historical_collection_not_proven:
    'Complete historical collection has not been established.',
  account_created_during_period: 'The account was created during this month.',
  account_created_after_period: 'The account did not exist during this month.',
  retention_gap: 'Some records are outside the available history.',
  current_partial_month: 'This month is still in progress.',
  current_partial_month_not_projected:
    'This month is still in progress; totals are not projected.',
  before_account_creation: 'The account did not exist during this month.',
  unpriced_hosted_usage: 'Some built-in model requests have no verified cost.',
};

/** Authenticated plan selection. Loading this component never changes a subscription. */
@customElement('billing-plan-comparison')
export class BillingPlanComparison extends LitElement {
  @state() private options: PlanChangeOptions | null = null;
  @state() private loading = true;
  @state() private busy: 'preview' | 'confirm' | 'checkout' | null = null;
  @state() private error = '';
  @state() private selectedPlan = '';
  @state() private interval: 'month' | 'year' = 'month';
  @state() private preview: PlanChangePreview | null = null;
  @state() private accepted = false;
  @state() private now = Date.now();
  @state() private result: PlanChangeResult | null = null;
  @state() private refreshRequired = false;
  @state() private pendingConfirmation: string | null = null;
  @state() private operatorRecovery = false;
  private recoveryKey: string | null = null;
  private revision = 0;
  private expiryTimer?: number;

  connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.revision++;
    window.clearTimeout(this.expiryTimer);
  }

  private isLegacy(plan: BillingPlan | null | undefined): boolean {
    return plan?.is_legacy ?? plan?.legacy ?? false;
  }

  private permissionChanged(allowed: boolean): void {
    this.dispatchEvent(
      new CustomEvent('billing-permission-changed', {
        detail: { canManageBilling: allowed },
        bubbles: true,
        composed: true,
      })
    );
  }

  async refresh(): Promise<void> {
    if (this.busy === 'confirm' || this.busy === 'checkout') return;
    const revision = ++this.revision;
    this.loading = true;
    this.preview = null;
    window.clearTimeout(this.expiryTimer);
    this.accepted = false;
    this.error = '';
    this.permissionChanged(false);
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/plan-change-options',
        { cache: 'no-store' }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const options = (await response.json()) as PlanChangeOptions;
      if (revision !== this.revision || !this.isConnected) return;
      const firstLoad = !this.options;
      this.options = options;
      if (firstLoad && options.current_subscription) {
        try {
          const raw = localStorage.getItem('accessToken')?.split('.')[1];
          const actor = raw
            ? JSON.parse(atob(raw.replace(/-/g, '+').replace(/_/g, '/'))).sub
            : null;
          if (typeof actor === 'string') {
            this.recoveryKey = `preloop:billing-change:${actor}:${options.current_subscription.id}`;
            const saved = JSON.parse(
              sessionStorage.getItem(this.recoveryKey) || 'null'
            );
            if (typeof saved?.preview_id === 'string') {
              this.pendingConfirmation = saved.preview_id;
              this.operatorRecovery = saved.operator_recovery === true;
            }
          }
        } catch {
          /* Recovery remains in memory if browser storage is unavailable. */
        }
      }
      if (firstLoad) {
        const params = new URLSearchParams(window.location.search);
        const requested = options.plans.find(
          (p) => p.id === params.get('plan') && !this.isLegacy(p)
        );
        if (requested) this.selectedPlan = requested.id;
        if (
          params.get('interval') === 'year' ||
          params.get('interval') === 'month'
        ) {
          this.interval = params.get('interval') as 'month' | 'year';
        }
      }
      this.refreshRequired = false;
      const candidates = options.plans.filter(
        (p) => !this.isLegacy(p) && p.id !== options.current_plan?.id
      );
      if (
        !options.plans.some(
          (p) => p.id === this.selectedPlan && !this.isLegacy(p)
        )
      )
        this.selectedPlan = candidates[0]?.id ?? '';
      this.permissionChanged(options.can_manage_billing === true);
    } catch (error) {
      if (revision !== this.revision) return;
      this.options = null;
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not load plan comparison.';
    } finally {
      if (revision === this.revision) {
        this.loading = false;
        this.busy = null;
      }
    }
  }

  private async responseError(response: Response): Promise<string> {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    if (typeof detail === 'string') return detail;
    if (typeof detail?.message === 'string') return detail.message;
    return response.status === 403
      ? 'Only a billing owner or account administrator can change this subscription.'
      : 'Could not verify the billing request. Refresh and try again.';
  }

  private choose(plan: string, interval = this.interval): void {
    if (
      this.busy === 'confirm' ||
      this.busy === 'checkout' ||
      this.pendingConfirmation
    )
      return;
    this.revision++;
    this.selectedPlan = plan;
    this.interval = interval;
    this.preview = null;
    window.clearTimeout(this.expiryTimer);
    this.accepted = false;
    this.busy = null;
    this.error = '';
    this.result = null;
  }

  private get target(): BillingPlan | undefined {
    return this.options?.plans.find((p) => p.id === this.selectedPlan);
  }

  private get salesLed(): boolean {
    return (
      this.target?.id === 'enterprise' ||
      (this.target?.id !== 'free' && this.target?.purchasable === false) ||
      (this.target?.price_monthly === null &&
        this.target?.price_annually === null)
    );
  }

  private get canAct(): boolean {
    return (
      !!this.options?.can_manage_billing &&
      !!this.options?.switching_enabled &&
      !this.loading &&
      !this.busy &&
      !this.refreshRequired &&
      !this.pendingConfirmation &&
      !!this.target &&
      !this.salesLed &&
      ((!!this.options.current_subscription && this.selectedPlan === 'free') ||
        !this.assessment()?.blockers?.length) &&
      (!this.options.current_subscription ||
        (!!this.options.current_subscription.revision &&
          Number.isFinite(
            this.options.current_subscription.total_amount_cents
          ))) &&
      !this.options?.current_subscription?.pending_change &&
      !this.options?.current_subscription?.cancel_at_period_end &&
      !(
        this.options?.current_subscription?.plan_id === this.selectedPlan &&
        this.options.current_subscription.interval === this.interval
      )
    );
  }

  private get expired(): boolean {
    const expires = Date.parse(this.preview?.expires_at ?? '');
    return (
      !Number.isFinite(expires) || expires <= Math.max(this.now, Date.now())
    );
  }

  private get validQuote(): boolean {
    const p = this.preview;
    return (
      !!p &&
      p.target.plan_id === this.selectedPlan &&
      p.target.interval === this.interval &&
      Number.isFinite(Date.parse(p.effective_at)) &&
      Number.isFinite(p.amount_due_now_cents) &&
      (p.proration_amount_cents === null ||
        Number.isFinite(p.proration_amount_cents)) &&
      Number.isFinite(p.target.total_amount_cents) &&
      Number.isFinite(p.current.total_amount_cents) &&
      p.amount_due_now_cents! >= 0 &&
      p.target.total_amount_cents! >= 0 &&
      /^[a-z]{3}$/i.test(p.currency) &&
      p.target.currency?.toLowerCase() === p.currency.toLowerCase() &&
      p.current.currency?.toLowerCase() === p.currency.toLowerCase() &&
      !!p.preview_id &&
      (p.timing === 'immediate' || p.timing === 'period_end')
    );
  }

  private async requestPreview(): Promise<void> {
    if (!this.canAct || !this.options?.current_subscription) return;
    const revision = ++this.revision;
    this.busy = 'preview';
    this.preview = null;
    this.accepted = false;
    this.error = '';
    this.result = null;
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/plan-change-preview',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target_plan_id: this.selectedPlan,
            interval: this.interval,
          }),
        }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const preview = (await response.json()) as PlanChangePreview;
      if (revision !== this.revision || !this.isConnected) return;
      this.preview = preview;
      this.now = Date.now();
      window.clearTimeout(this.expiryTimer);
      const delay = Date.parse(preview.expires_at) - this.now;
      if (Number.isFinite(delay) && delay > 0) {
        this.expiryTimer = window.setTimeout(
          () => {
            this.now = Date.now();
          },
          Math.min(delay + 10, 2147483647)
        );
      }
    } catch (error) {
      if (revision === this.revision)
        this.error =
          error instanceof Error ? error.message : 'Preview unavailable.';
    } finally {
      if (revision === this.revision) this.busy = null;
    }
  }

  private async confirm(): Promise<void> {
    if (
      !this.canAct ||
      !this.preview ||
      this.expired ||
      !this.validQuote ||
      !this.accepted ||
      this.preview.blockers.length
    )
      return;
    this.pendingConfirmation = this.preview.preview_id;
    this.persistRecovery();
    await this.sendConfirmation();
  }

  private persistRecovery(): void {
    if (!this.recoveryKey) return;
    try {
      if (this.pendingConfirmation)
        sessionStorage.setItem(
          this.recoveryKey,
          JSON.stringify({
            preview_id: this.pendingConfirmation,
            operator_recovery: this.operatorRecovery,
          })
        );
      else sessionStorage.removeItem(this.recoveryKey);
    } catch {
      /* The current page still retains the original command. */
    }
  }

  private async retryConfirmation(): Promise<void> {
    if (
      !this.pendingConfirmation ||
      this.busy ||
      !this.options?.can_manage_billing ||
      this.operatorRecovery
    )
      return;
    await this.sendConfirmation();
  }

  private async sendConfirmation(): Promise<void> {
    const previewId = this.pendingConfirmation!;
    this.busy = 'confirm';
    this.error = '';
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/plan-change-confirm',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ preview_id: previewId }),
        }
      );
      if (!response.ok) {
        const body = await response
          .clone()
          .json()
          .catch(() => ({}));
        const code = body.detail?.code;
        if (body.detail?.operation_started === false)
          this.pendingConfirmation = null;
        this.operatorRecovery = code === 'recovery_required';
        throw new Error(await this.responseError(response));
      }
      const result = (await response.json()) as PlanChangeResult;
      if (
        !['scheduled', 'applied'].includes(result.status) ||
        !result.operation_id
      )
        throw new Error('The change outcome could not be verified.');
      this.result = result;
      this.pendingConfirmation = null;
      this.operatorRecovery = false;
      this.dispatchEvent(
        new CustomEvent('billing-subscription-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not verify the change.';
      if (!this.pendingConfirmation)
        this.error +=
          ' Refresh subscription status before requesting another preview.';
    } finally {
      this.persistRecovery();
      this.refreshRequired = true;
      this.preview = null;
      this.accepted = false;
      this.busy = null;
    }
  }

  private navigate(url: string): void {
    window.location.href = url;
  }

  private async checkout(): Promise<void> {
    if (
      !this.canAct ||
      this.options?.current_subscription ||
      this.target?.id === 'free'
    )
      return;
    this.busy = 'checkout';
    this.error = '';
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/create-checkout-session',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            plan_id: this.selectedPlan,
            interval: this.interval,
          }),
        }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const result = await response.json();
      if (result.action !== 'redirect' || typeof result.url !== 'string') {
        throw new Error(
          'Checkout is not available. Refresh subscription status.'
        );
      }
      this.navigate(result.url);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not open checkout.';
    } finally {
      this.busy = null;
    }
  }

  private money(cents: number | null | undefined, currency = 'usd'): string {
    if (cents === null || cents === undefined || !Number.isFinite(cents))
      return 'Unavailable';
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency,
      }).format(cents / 100);
    } catch {
      return 'Unavailable';
    }
  }
  private count(value: number | null | undefined): string {
    if (value == null || !Number.isFinite(value)) return 'Unknown';
    return new Intl.NumberFormat(undefined).format(value);
  }
  private date(value: string): string {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? 'Unknown'
      : date.toLocaleString(undefined, {
          dateStyle: 'medium',
          timeStyle: 'short',
        });
  }
  private month(value: string): string {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? 'Unknown month'
      : date.toLocaleDateString(undefined, {
          month: 'long',
          year: 'numeric',
          timeZone: 'UTC',
        });
  }
  private notice(value: BillingNotice | string): string {
    return typeof value === 'string' ? value : value.message;
  }
  private feature(plan: BillingPlan | null | undefined, key: string): string {
    if (
      key === 'hosted_models_monthly_limit_usd' &&
      plan?.features?.hosted_credit_one_time_usd != null
    ) {
      const credit = plan.features.hosted_credit_one_time_usd;
      return typeof credit === 'number'
        ? `${this.money(credit * 100)} one-time credit`
        : 'One-time credit';
    }
    const value = plan?.features?.[key];
    if (value === undefined || value === null) return 'Not specified';
    if (value === -1) return key === 'retention_days' ? 'Custom' : 'Unlimited';
    if (typeof value !== 'number') return String(value);
    if (key === 'hosted_models_monthly_limit_usd')
      return `${this.money(value * 100)} / month`;
    if (key === 'byok_ingest_tokens_monthly')
      return `${this.count(value)} tokens / month`;
    if (key === 'retention_days')
      return value === 183
        ? '6 months (183 days)'
        : value === 365
          ? '1 year'
          : value === 730
            ? '2 years'
            : `${this.count(value)} days`;
    return this.count(value);
  }
  private assessment(): PlanAssessment | undefined {
    return this.options?.assessments.find(
      (a) => a.plan_id === this.selectedPlan
    );
  }
  private fitLabel(assessment?: PlanAssessment): string {
    if (assessment?.fit === 'blocked')
      return 'Current account capacity exceeds this plan';
    if (assessment?.fit === 'exceeds' || assessment?.fit === 'exceeded')
      return 'Exceeds one or more observed limits';
    const completed =
      this.options?.monthly_usage.filter((m) => !m.is_partial) ?? [];
    const proven =
      completed.length === 3 &&
      completed.every(
        (m) =>
          m.coverage === 'complete' &&
          m.observed_byok_tokens != null &&
          m.observed_hosted_cost_usd != null
      );
    return assessment?.fit === 'fits' && proven
      ? 'Within the recorded monthly limits'
      : 'Not enough evidence to confirm a fit';
  }

  private renderHistory() {
    const o = this.options!;
    const assessment = this.assessment();
    return html`
      ${
        o.hosted_credit
          ? html`
              <h3>Current built-in model balance</h3>
              <p>
                ${o.hosted_credit.coverage === 'unknown' ? 'Your historical hosted balance has not been verified. An unavailable balance does not mean fresh credit.' : html`Available ${o.hosted_credit.one_time_credit_usd != null ? 'one-time credit' : 'this UTC calendar month'}: ${this.money((o.hosted_credit.one_time_credit_usd != null ? o.hosted_credit.remaining_credit_usd : o.hosted_credit.month_remaining_usd) == null ? null : (o.hosted_credit.one_time_credit_usd != null ? o.hosted_credit.remaining_credit_usd! : o.hosted_credit.month_remaining_usd!) * 100)}.`}
              </p>
              ${(o.hosted_credit.lifetime_reserved_usd ?? 0) > 0 ? html`<p>${this.money(o.hosted_credit.lifetime_reserved_usd! * 100)} is reserved for calls in progress or awaiting verified provider charges. Reserved credit is not available for another call; it is released or settled when the charge is confirmed.</p>` : nothing}
              ${o.hosted_credit.extra_spending_enabled === false ? html`<p>Extra spending is off. When the included balance is exhausted, built-in model calls stop. Calls using your own provider keys continue.</p>` : nothing}
            `
          : nothing
      }
      <h3>Would this plan cover your usage?</h3>
      <p class="fit" data-testid="fit">${this.fitLabel(assessment)}</p>
      <p>
        The previous three completed calendar months are shown separately from
        the current partial month. Missing records are not zero usage.
        Observations do not predict future usage.
      </p>
      <div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="Monthly usage comparison"
      >
        <table>
          <caption>
            Recorded usage and selected plan limits. Months use UTC.
          </caption>
          <thead>
            <tr>
              <th scope="col">Month</th>
              <th scope="col">Available history</th>
              <th scope="col">BYOK analysis tokens</th>
              <th scope="col">Built-in model cost</th>
            </tr>
          </thead>
          <tbody>
            ${o.monthly_usage.map((m) => this.renderMonth(m))}
          </tbody>
        </table>
      </div>
      <p>
        Selected plan:
        ${this.feature(this.target, 'byok_ingest_tokens_monthly')} for BYOK
        analysis;
        ${this.feature(this.target, 'hosted_models_monthly_limit_usd')} for
        built-in models. Provider charges on your own keys are separate and are
        never marked up by Preloop.
      </p>
      ${this.target?.features.hosted_credit_one_time_usd != null ? html`<p>The Free hosted credit is granted once across the account lifetime. It does not reset each month or when you change plans. Remaining lifetime credit: ${this.money(o.hosted_credit?.remaining_credit_usd == null ? null : o.hosted_credit.remaining_credit_usd * 100)}.</p>` : nothing}
      <p>
        Current users:
        <strong>${this.count(o.current_usage.active_users)}</strong>; pending
        invitations:
        <strong>${this.count(o.current_usage.pending_invitations)}</strong>;
        current agents:
        <strong>${this.count(o.current_usage.active_agents)}</strong>.
      </p>
      <p>
        Historical user peak:
        ${this.count(o.current_usage.historical_seat_peak)}. Historical agent
        peak: ${this.count(o.current_usage.historical_agent_peak)}. Current
        counts do not establish past peaks.
      </p>
      ${
        assessment?.blockers?.length
          ? html`<ul class="warning">
              ${assessment.blockers.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${
        assessment?.advisories?.length
          ? html`<ul>
              ${assessment.advisories.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
    `;
  }
  private observedLimit(m: BillingMonth, hosted: boolean): string {
    if (hosted && this.target?.features.hosted_credit_one_time_usd != null)
      return 'Lifetime credit, not a monthly allowance';
    const observed = hosted
      ? m.observed_hosted_cost_usd
      : m.observed_byok_tokens;
    const limit =
      this.target?.features[
        hosted
          ? 'hosted_models_monthly_limit_usd'
          : 'byok_ingest_tokens_monthly'
      ];
    if (limit === -1) return 'No plan quota';
    if (typeof limit !== 'number' || observed == null) return 'Cannot assess';
    if (observed > limit)
      return hosted ? 'Above included allowance' : 'Above selected quota';
    return m.coverage === 'complete'
      ? 'Within observed limit'
      : 'Cannot confirm full-month fit';
  }
  private renderMonth(m: BillingMonth) {
    const reason = (m.coverage_reasons ?? [])
      .map(
        (r) =>
          COVERAGE_REASONS[r] ??
          'Complete coverage is not established for this month.'
      )
      .join(' ');
    return html`<tr>
      <th scope="row">
        ${this.month(m.period_start)}${m.is_partial ? html`<br /><span class="muted">Current partial month</span>` : nothing}
      </th>
      <td>
        ${m.coverage === 'complete' ? 'Complete' : m.coverage === 'partial' ? 'Incomplete' : m.coverage === 'not_applicable' ? 'Account did not exist' : 'Unknown'}${reason ? html`<br /><small>${reason}</small>` : nothing}
      </td>
      <td>
        ${this.count(m.observed_byok_tokens)}<br /><small
          >${this.observedLimit(m, false)}</small
        >${m.coverage !== 'complete' && m.observed_byok_tokens != null ? html`<br /><small>Observed only</small>` : nothing}
      </td>
      <td>
        ${this.money(m.observed_hosted_cost_usd == null ? null : m.observed_hosted_cost_usd * 100)}<br /><small
          >${this.observedLimit(m, true)}</small
        >${m.coverage !== 'complete' && m.observed_hosted_cost_usd != null ? html`<br /><small>Observed only</small>` : nothing}
      </td>
    </tr>`;
  }
  private renderLimits() {
    const current = this.options!.current_plan;
    const target = this.target;
    const rows = [
      ['max_users', 'Users'],
      ['max_agents', 'Agents'],
      ['byok_ingest_tokens_monthly', 'BYOK analysis'],
      ['hosted_models_monthly_limit_usd', 'Built-in model allowance'],
      ['retention_days', 'Analytics history'],
    ];
    const capabilities = Object.keys(CAPABILITIES).filter(
      (key) =>
        current?.capabilities?.includes(key) ||
        target?.capabilities?.includes(key)
    );
    return html`<div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="Changed plan limits"
      >
        <table>
          <caption>
            What changes
          </caption>
          <thead>
            <tr>
              <th scope="col">Included</th>
              <th scope="col">${current?.name ?? 'Current plan'}</th>
              <th scope="col">${target?.name ?? 'Selected plan'}</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              ([key, label]) =>
                html`<tr>
                  <th scope="row">${label}</th>
                  <td>${this.feature(current, key)}</td>
                  <td>${this.feature(target, key)}</td>
                </tr>`
            )}
            ${capabilities.map(
              (key) =>
                html`<tr>
                  <th scope="row">${CAPABILITIES[key]}</th>
                  <td>
                    ${current?.capabilities?.includes(key) ? 'Included' : 'Not included'}
                  </td>
                  <td>
                    ${target?.capabilities?.includes(key) ? 'Included' : 'Not included'}
                  </td>
                </tr>`
            )}
          </tbody>
        </table>
      </div>
      ${target?.seat_addon ? html`<p>Extra users: ${this.money(target.seat_addon.price_per_user_monthly * 100)} per user monthly, or ${this.money(target.seat_addon.price_per_user_annually * 100)} annually, up to ${target.seat_addon.max_users} users. The quote includes any required extra users.</p>` : nothing}
      <p>
        Exhausting a BYOK analysis quota reduces analytics detail. The gateway,
        firewall, approvals and budgets continue enforcing your policies.
        Built-in model spend limits are separate.
      </p>
      <p>
        Analytics history controls access and storage for usage and
        runtime-session analytics. Older analytics are periodically removed;
        longer grandfathered commitments remain protected. Audit and evidence
        retention is managed
        separately.${this.options!.storage_retention?.minimum_days != null ? html` Stored audit and evidence records have a minimum retention of ${this.options!.storage_retention.minimum_days} days under your account policy.` : nothing}${this.options!.storage_retention?.legal_holds_override ? ' Legal holds can retain records longer.' : ''}
      </p>`;
  }
  private renderPreview() {
    const p = this.preview;
    if (!p) return nothing;
    return html`<section
      class="preview"
      aria-label="Confirm plan change"
      aria-live="polite"
    >
      <h3>Review before changing your subscription</h3>
      <dl>
        <dt>Current recurring subtotal</dt>
        <dd>
          ${p.current.name}:
          ${this.money(p.current.total_amount_cents, p.current.currency)} /
          ${p.current.interval}${p.current.quantity > 1 ? ` (${p.current.quantity} users)` : ''}
        </dd>
        <dt>New recurring subtotal</dt>
        <dd>
          ${p.target.name}:
          ${this.money(p.target.total_amount_cents, p.target.currency)} /
          ${p.target.interval}${p.target.addon_quantity ? ` (includes ${p.target.addon_quantity} extra users)` : ''}
        </dd>
        <dt>Effective date</dt>
        <dd>
          ${this.date(p.effective_at)}
          (${p.timing === 'period_end' ? 'at the end of your current billing period' : 'immediately after confirmation'})
        </dd>
        <dt>Proration</dt>
        <dd>
          ${p.proration_amount_cents === null ? 'Not separately itemized; included in the verified amount due now.' : this.money(p.proration_amount_cents, p.currency)}
        </dd>
        <dt>Due now</dt>
        <dd>${this.money(p.amount_due_now_cents, p.currency)}</dd>
        <dt>Quote expires</dt>
        <dd>${this.date(p.expires_at)}</dd>
      </dl>
      <p>
        Recurring subtotals exclude discounts and tax. Due now is the amount
        returned in the billing preview.
      </p>
      <p>
        Your current subscription continues until the effective date. Confirming
        a change does not cancel your account or require signing up again.
      </p>
      ${this.isLegacy(this.options?.current_plan) ? html`<p class="warning">You are choosing to leave Legacy Teams pricing. Returning to this withdrawn plan is not offered through self-service.</p>` : nothing}
      ${
        p.blockers.length
          ? html`<ul class="warning">
              ${p.blockers.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${
        p.advisories.length
          ? html`<ul>
              ${p.advisories.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${!this.validQuote ? html`<p role="alert">A complete price and effective date could not be verified. Request a new preview.</p>` : nothing}
      ${this.expired ? html`<p role="alert">This preview has expired. Request a new preview before confirming.</p>` : nothing}
      <label class="consent"
        ><input
          type="checkbox"
          data-testid="consent"
          .checked=${this.accepted}
          ?disabled=${!!this.busy || this.expired || !this.validQuote || !!p.blockers.length}
          @change=${(e: Event) => {
            this.accepted = (e.target as HTMLInputElement).checked;
          }}
        />
        I have reviewed the price, changed limits, available usage history and
        effective date, and I want to make this change.</label
      >
      <button
        data-testid="confirm"
        ?disabled=${!this.canAct || !this.accepted || this.expired || !this.validQuote || !!p.blockers.length}
        @click=${this.confirm}
      >
        ${this.busy === 'confirm' ? 'Confirming…' : p.timing === 'period_end' ? 'Confirm scheduled change' : 'Confirm plan change'}
      </button>
    </section>`;
  }

  render() {
    const o = this.options;
    return html`<section
      aria-labelledby="compare-title"
      aria-busy=${this.loading || !!this.busy}
    >
      <div class="heading">
        <h2 id="compare-title">Compare and change your cloud plan</h2>
        <button
          class="secondary"
          @click=${this.refresh}
          ?disabled=${this.loading || this.busy === 'confirm' || this.busy === 'checkout'}
        >
          Refresh subscription status
        </button>
      </div>
      ${this.error ? html`<p class="warning" role="alert">${this.error}</p>` : nothing}
      ${
        this.pendingConfirmation
          ? html`<section
              class="warning"
              aria-label="Unconfirmed billing operation"
            >
              <p>
                The outcome of your confirmed change is not yet known. Your
                original confirmation is retained; refreshing prices does not
                resolve it.
              </p>
              ${
                this.operatorRecovery
                  ? html`<p>
                      Billing reconciliation is required.
                      <a href="mailto:support@preloop.ai">Contact support</a>
                      before attempting another change.
                    </p>`
                  : html`<button
                        data-testid="recover"
                        ?disabled=${!!this.busy || !o?.can_manage_billing}
                        @click=${this.retryConfirmation}
                      >
                        Check or complete the original change
                      </button>
                      <p>
                        This sends the same confirmation again, using its
                        original operation identifier.
                      </p>`
              }
            </section>`
          : nothing
      }
      ${this.result ? html`<p class="success" role="status">${this.result.status === 'scheduled' ? 'Plan change scheduled' : 'Plan changed'}: ${this.result.plan_id}, effective ${this.date(this.result.effective_at)}. Refresh to see your subscription.</p>` : nothing}
      ${this.loading ? html`<p role="status">Loading current prices and usage coverage…</p>` : nothing}
      ${
        !this.loading && o
          ? html`
              ${!o.can_manage_billing ? html`<p class="warning">Only a billing owner or account administrator can change this subscription. You can review the comparison.</p>` : nothing}
              ${!o.switching_enabled ? html`<p class="warning">Plan changes are currently unavailable. Your existing subscription is unchanged. <a href="mailto:sales@preloop.ai">Contact support</a> if you need help.</p>` : nothing}
              ${
                o.warnings?.length
                  ? html`<ul class="warning">
                      ${o.warnings.map((n) => html`<li>${this.notice(n)}</li>`)}
                    </ul>`
                  : nothing
              }
              ${o.current_subscription ? html`<p><strong>${o.current_plan?.name ?? 'Current plan details unavailable'}</strong>${this.isLegacy(o.current_plan) ? ' (grandfathered)' : ''}. Current recurring subtotal before discounts and tax: <strong>${this.money(o.current_subscription.total_amount_cents, o.current_subscription.currency)} / ${o.current_subscription.interval ?? 'unverified period'}</strong>${this.isLegacy(o.current_plan) ? html` (${this.money(o.current_subscription.unit_amount_cents, o.current_subscription.currency)} per user, ${this.count(o.current_subscription.quantity)} users). Your grandfathered per-user rate stays until you choose to change plans.` : '.'}</p>` : html`<p>You are comparing cloud plans. The free open-source self-hosted edition has its own terms and is not subject to these cloud plan limits.</p>`}
              ${o.current_subscription?.pending_change || o.current_subscription?.cancel_at_period_end ? html`<p class="warning">A subscription change is already scheduled. Review it before choosing another change.</p>` : nothing}
              <div class="selectors">
                <label
                  >Compare with<select
                    data-testid="plan"
                    .value=${this.selectedPlan}
                    ?disabled=${this.busy === 'confirm' || this.busy === 'checkout' || !!this.pendingConfirmation}
                    @change=${(e: Event) => this.choose((e.target as HTMLSelectElement).value)}
                  >
                    ${o.plans.filter((p) => !this.isLegacy(p)).map((p) => html`<option value=${p.id}>${p.name}</option>`)}
                  </select></label
                >
                <label
                  >Billing period<select
                    data-testid="interval"
                    .value=${this.interval}
                    ?disabled=${this.busy === 'confirm' || this.busy === 'checkout' || !!this.pendingConfirmation}
                    @change=${(e: Event) => this.choose(this.selectedPlan, (e.target as HTMLSelectElement).value as 'month' | 'year')}
                  >
                    <option value="month">Monthly</option>
                    <option value="year">Annually</option>
                  </select></label
                >
              </div>
              ${
                this.target
                  ? html`
                      ${this.renderLimits()}${this.renderHistory()}
                      ${
                        this.salesLed
                          ? html`<p>
                                Enterprise starts at $30,000 per year for a
                                scoped deployment with up to 100 users.
                                Deployment and support requirements need an
                                agreed quote.
                              </p>
                              <a class="contact" href="/request-demo"
                                >Contact us about Enterprise</a
                              >`
                          : html`
                              ${
                                o.current_subscription
                                  ? html`<button
                                      data-testid="preview"
                                      ?disabled=${!this.canAct}
                                      @click=${this.requestPreview}
                                    >
                                      ${this.busy === 'preview' ? 'Preparing price preview…' : 'Preview price and effective date'}
                                    </button>`
                                  : html`<p>
                                        ${this.target.name}:
                                        ${this.money((this.interval === 'year' ? this.target.price_annually : this.target.price_monthly) == null ? null : (this.interval === 'year' ? this.target.price_annually! : this.target.price_monthly!) * 100)}
                                        / ${this.interval}. Secure checkout
                                        shows the final amount and any taxes
                                        before you subscribe.
                                      </p>
                                      <button
                                        data-testid="checkout"
                                        ?disabled=${!this.canAct || this.target.id === 'free'}
                                        @click=${this.checkout}
                                      >
                                        Continue to secure checkout
                                      </button>`
                              }
                            `
                      }${this.renderPreview()}
                    `
                  : html`<p>
                      No other cloud plans are available.
                      <a href="/request-demo">Contact us</a> for a custom
                      deployment.
                    </p>`
              }
            `
          : nothing
      }
    </section>`;
  }

  static styles = css`
    :host {
      display: block;
      color: var(--sl-color-neutral-900);
    }
    section {
      display: grid;
      gap: 1rem;
    }
    h2,
    h3,
    p {
      margin: 0;
    }
    p,
    li {
      line-height: 1.6;
    }
    h2 {
      font-size: 1.2rem;
    }
    h3 {
      font-size: 1.05rem;
    }
    .heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .selectors {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
    }
    label {
      display: grid;
      gap: 0.5rem;
      font-weight: 500;
    }
    select,
    button {
      font: inherit;
      min-height: 44px;
      border-radius: 0.4rem;
      padding: 0.55rem 0.8rem;
    }
    select {
      border: 1px solid var(--sl-color-neutral-300);
      color: inherit;
      background: var(--sl-color-neutral-0);
    }
    button {
      cursor: pointer;
      color: var(--sl-color-neutral-0);
      background: var(--sl-color-primary-600);
      border: 1px solid var(--sl-color-primary-600);
      justify-self: start;
    }
    button.secondary {
      background: transparent;
      color: inherit;
      border-color: var(--sl-color-neutral-300);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    :focus-visible {
      outline: 2px solid var(--sl-color-primary-600);
      outline-offset: 3px;
    }
    a {
      color: var(--sl-color-primary-700);
    }
    .contact {
      padding: 0.7rem 0;
    }
    .table-scroll {
      overflow-x: auto;
      max-width: 100%;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.9rem;
    }
    caption {
      text-align: left;
      font-weight: 600;
      margin-bottom: 0.6rem;
    }
    th,
    td {
      padding: 0.65rem;
      border-bottom: 1px solid var(--sl-color-neutral-200);
      vertical-align: top;
      min-width: 8rem;
    }
    small,
    .muted {
      color: var(--sl-color-neutral-600);
    }
    small {
      display: inline-block;
      max-width: 24rem;
    }
    .warning {
      padding: 0.8rem;
      background: var(--sl-color-warning-50);
      border-left: 3px solid var(--sl-color-warning-600);
    }
    ul.warning {
      padding-left: 2rem;
    }
    .success {
      padding: 0.8rem;
      background: var(--sl-color-success-50);
    }
    .fit {
      font-weight: 600;
    }
    .preview {
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.5rem;
      padding: 1rem;
    }
    dl {
      display: grid;
      grid-template-columns: minmax(8rem, 1fr) 2fr;
      gap: 0.6rem;
      margin: 0;
    }
    dt {
      font-weight: 600;
    }
    dd {
      margin: 0;
    }
    .consent {
      display: flex;
      align-items: flex-start;
      font-weight: 400;
      line-height: 1.6;
    }
    input[type='checkbox'] {
      width: 20px;
      height: 20px;
      margin-top: 0.2rem;
      flex-shrink: 0;
    }
    @media (max-width: 560px) {
      dl {
        grid-template-columns: 1fr;
      }
      dd {
        margin-bottom: 0.5rem;
      }
      .selectors label {
        width: 100%;
      }
    }
  `;
}
