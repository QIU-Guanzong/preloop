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
