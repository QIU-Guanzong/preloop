/** Account-scoped pricing API. Monetary quote amounts are integer minor units. */
export interface BillingPlan {
  id: string;
  name: string;
  price_monthly?: number | null;
  price_annually?: number | null;
  pricing_model?: 'bracket' | 'per_seat';
  legacy?: boolean;
  is_legacy?: boolean;
  purchasable?: boolean;
  features: Record<string, number | boolean | string | null>;
  capabilities?: string[];
  seat_addon?: {
    price_per_user_monthly: number;
    price_per_user_annually: number;
    max_users: number;
  } | null;
}

export interface BillingNotice {
  code: string;
  message: string;
}
export interface BillingMonth {
  period_start: string;
  period_end: string;
  is_partial: boolean;
  coverage: 'complete' | 'partial' | 'unknown' | 'not_applicable';
  coverage_reasons: string[];
  observed_byok_tokens: number | null;
  observed_hosted_cost_usd: number | null;
  request_count: number | null;
}
export interface PlanAssessment {
  plan_id: string;
  fit: 'fits' | 'exceeds' | 'exceeded' | 'blocked' | 'unknown';
  blockers: BillingNotice[];
  advisories: BillingNotice[];
  months: {
    period_start: string;
    byok_status: string;
    hosted_status: string;
  }[];
}
/** One reason a plan does not fit, or one consequence of choosing it. */
export interface PlanBlocker {
  kind: 'members' | 'agents' | 'ingest' | 'retention';
  current: number | string | null;
  limit: number | null;
  message: string;
}
/** What the target plan's analytics history window does to existing records. */
export interface PlanRetention {
  oldest_record_at: string | null;
  oldest_record_class: string | null;
  target_days: number | null;
  cutoff_at: string | null;
  affected: boolean;
  protected_by_floor: boolean;
  floor_days: number | null;
  legal_hold: boolean;
  message: string | null;
  benefit_message: string | null;
}
/**
 * The server's answer for one candidate plan. Every sentence shown to the
 * reader is composed server side, where the catalog limits and the account's
 * measured state both live; the console decides only where to put them.
 *
 * One named carve-out: the contact line for a quote-only plan. The server
 * composes it in `contact_message` when it has an opinion, and the console
 * composes "X is priced per deployment." only when that field is absent,
 * which is what a deployment without the billing plugin sends. `contact_url`
 * is a destination rather than a sentence, and the console restricts it to a
 * same-origin path or an http(s) URL before it reaches an `href`.
 */
export interface PlanEligibility {
  plan_id: string;
  name: string;
  eligible: boolean;
  purchasable: boolean;
  contact_url: string | null;
  contact_message?: string | null;
  requires_period: boolean;
  is_current: boolean;
  blockers: PlanBlocker[];
  warnings: PlanBlocker[];
  retention: PlanRetention;
}
export interface BillingSubscription {
  id: string;
  plan_id: string;
  status: string;
  interval: 'month' | 'year';
  quantity: number;
  currency: string;
  unit_amount_cents: number | null;
  total_amount_cents: number | null;
  current_period_end: string;
  cancel_at_period_end: boolean;
  legacy: boolean;
  revision: string;
  pending_change: {
    plan_id?: string;
    target_plan_id?: string;
    effective_at?: string;
  } | null;
}
export interface PlanChangeOptions {
  can_manage_billing: boolean;
  switching_enabled: boolean;
  current_subscription: BillingSubscription | null;
  current_plan: BillingPlan | null;
  plans: BillingPlan[];
  monthly_usage: BillingMonth[];
  current_usage: {
    active_users: number | null;
    pending_invitations: number | null;
    active_agents: number | null;
    historical_seat_peak: number | null;
    historical_agent_peak: number | null;
  };
  assessments: PlanAssessment[];
  plan_eligibility?: PlanEligibility[];
  account_state?: {
    members: number | null;
    active_users: number | null;
    pending_invitations: number | null;
    active_agents: number | null;
    ingest_tokens_this_month: number | null;
    oldest_record_at: string | null;
    oldest_record_class: string | null;
    retention_floor_days: number | null;
    legal_hold: boolean;
  };
  storage_retention: {
    source: string;
    minimum_days: number | null;
    legal_holds_override: boolean;
  };
  hosted_credit?: {
    one_time_credit_usd: number | null;
    lifetime_usage_usd: number | null;
    remaining_credit_usd: number | null;
    lifetime_reserved_usd?: number | null;
    monthly_allowance_usd?: number | null;
    month_usage_usd?: number | null;
    month_reserved_usd?: number | null;
    month_remaining_usd?: number | null;
    extra_spending_enabled?: boolean;
    extra_spending_cap_usd?: number;
    coverage?: 'known' | 'unknown';
  };
  warnings: (BillingNotice | string)[];
}
export interface PlanQuote {
  plan_id: string;
  name: string;
  interval: 'month' | 'year';
  quantity: number;
  unit_amount_cents: number | null;
  total_amount_cents: number | null;
  currency: string;
  features: BillingPlan['features'];
  capabilities?: string[];
  addon_quantity?: number;
}
export interface PlanChangePreview {
  preview_id: string;
  expires_at: string;
  current: PlanQuote;
  target: PlanQuote;
  timing: 'immediate' | 'period_end';
  effective_at: string;
  proration_amount_cents: number | null;
  amount_due_now_cents: number | null;
  currency: string;
  assessment: PlanAssessment;
  blockers: BillingNotice[];
  advisories: BillingNotice[];
  confirmation_required: boolean;
}
export interface PlanChangeResult {
  status: 'scheduled' | 'applied';
  plan_id: string;
  effective_at: string;
  operation_id: string;
}
