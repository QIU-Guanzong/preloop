/**
 * What a flow execution offers, by run status.
 *
 * The list and the execution page each carried their own copy of the retry
 * predicate and their own labels ("Retry run" against "Retry"), and the list
 * offered Open session on runs that never had one, which lands on a search
 * with no results.
 */
import { confirmDialog } from '../components/confirm-dialog';
import type { ResourceAction } from '../components/resource-actions';
import { RUNNING_STATUSES } from '../utils/execution';
import { defineActions, type ActionContext } from './registry';

/**
 * Statuses the stop command accepts.
 *
 * The same four `POST /flows/executions/{id}/command` treats as stoppable,
 * including PENDING: a run that is queued and has no runtime yet is exactly
 * the one an operator wants to call off, and the backend completes that stop
 * durably (`cancel_unstarted_stop`), so it can never be claimed later.
 */
export const STOPPABLE_EXECUTION_STATUSES: ReadonlySet<string> =
  RUNNING_STATUSES;

/** Statuses the retry endpoint accepts. */
export const RETRYABLE_EXECUTION_STATUSES = new Set([
  'FAILED',
  'STOPPED',
  'TIMEOUT',
  'CANCELLED',
]);

export interface FlowExecutionActionResource {
  id: string;
  status: string;
  /** Set once the run opened a runtime session worth linking to. */
  agent_session_reference?: string | null;
  flow_id?: string | null;
}

export interface FlowExecutionActionContext extends ActionContext {
  busy?: boolean;
  /** Offered on a list row, where the execution page is somewhere else. */
  includeOpen?: boolean;
  /** Offered on the execution page, which knows which flow it belongs to. */
  includeViewFlow?: boolean;
  /**
   * Where "Open session" goes. The execution page sends the operator to the
   * session record in the sessions list; a list row sends them to the run's
   * own transcript, which is the same conversation one page closer.
   */
  sessionHref?: (execution: FlowExecutionActionResource) => string;
  onCancel?: (execution: FlowExecutionActionResource) => void;
  onRetry?: (execution: FlowExecutionActionResource) => void;
}

export function isExecutionRunning(
  execution: FlowExecutionActionResource
): boolean {
  return RUNNING_STATUSES.has(execution.status);
}

/** Whether the stop command will act on this run. */
export function canStopExecution(
  execution: FlowExecutionActionResource
): boolean {
  return STOPPABLE_EXECUTION_STATUSES.has(execution.status);
}

/**
 * The one confirmation asked before a run is stopped.
 *
 * Stopping destroys work in progress and cannot be undone from the console,
 * so every surface that offers it asks the same question in the same words.
 */
export function confirmStopExecution(execution: {
  flow_name?: string | null;
}): Promise<boolean> {
  return confirmDialog({
    title: 'Cancel run',
    message: `Stop the run of "${execution.flow_name || 'this flow'}"?`,
    detail:
      'The agent stops where it is. Work already done is kept in the run, but the run does not finish, and it cannot be resumed \u2014 only retried from the start.',
    confirmLabel: 'Cancel run',
    cancelLabel: 'Keep running',
    variant: 'danger',
  });
}

export function canRetryExecution(
  execution: FlowExecutionActionResource
): boolean {
  return RETRYABLE_EXECUTION_STATUSES.has(execution.status);
}

export function executionDetailUrl(
  execution: FlowExecutionActionResource
): string {
  return `/console/flows/executions/${encodeURIComponent(execution.id)}`;
}

/** The session this run opened, found by id in the sessions list. */
export function executionSessionUrl(
  execution: FlowExecutionActionResource
): string {
  return `/console/runtime-sessions?query=${encodeURIComponent(execution.id)}`;
}

export function flowExecutionActions(
  execution: FlowExecutionActionResource,
  ctx: FlowExecutionActionContext = {}
): ResourceAction[] {
  const busy = ctx.busy === true;
  return defineActions(
    execution,
    [
      ctx.includeOpen
        ? {
            id: 'open',
            label: 'Open',
            icon: 'box-arrow-up-right',
            href: executionDetailUrl(execution),
          }
        : null,
      {
        id: 'retry',
        label: 'Retry run',
        icon: 'arrow-repeat',
        loading: busy,
        available: canRetryExecution,
        onClick: ctx.onRetry ? () => ctx.onRetry!(execution) : undefined,
      },
      {
        id: 'open-session',
        label: 'Open session',
        icon: 'chat-left-text',
        available: (item) => Boolean(item.agent_session_reference),
        href: ctx.sessionHref
          ? ctx.sessionHref(execution)
          : executionSessionUrl(execution),
      },
      ctx.includeViewFlow
        ? {
            id: 'view-flow',
            label: 'View flow',
            icon: 'diagram-3',
            available: (item) => Boolean(item.flow_id),
            href: `/console/flows/${encodeURIComponent(
              execution.flow_id || ''
            )}`,
          }
        : null,
      // Stopping a run destroys work in progress, so it is destructive and
      // sits apart from the everyday actions.
      {
        id: 'cancel',
        label: 'Cancel run',
        icon: 'x-circle',
        variant: 'danger',
        outline: true,
        separated: true,
        available: canStopExecution,
        onClick: ctx.onCancel ? () => ctx.onCancel!(execution) : undefined,
      },
    ],
    ctx
  );
}
