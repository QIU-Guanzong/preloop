/**
 * The delegation allowlist as the flow editor sees it.
 *
 * `Flow.callable_flows` (issue #627) names the flows one flow is permitted to
 * run, each with optional per child ceilings. The column fails closed: an
 * unset value and an empty list mean the same thing, "this flow may call
 * nothing at all", so the console never has to distinguish them.
 *
 * The server is the authority on every rule here. These helpers exist so the
 * form can refuse a save it already knows the API would reject, and so the
 * refusal names the entry it came from instead of failing generically.
 */

/** The MCP server the delegation tool is served from. */
export const DELEGATION_TOOL_SERVER = 'preloop-mcp';

/** The builtin tool that spends the allowlist (issue #630). */
export const DELEGATION_TOOL_NAME = 'run_flow';

/** Mirrors `CALLABLE_FLOWS_MAX_ENTRIES` in the flow schema. */
export const CALLABLE_FLOWS_MAX_ENTRIES = 50;

/** One allowlist row: a flow that may be called, with its ceilings. */
export interface CallableFlowEntry {
  /** Slug or name of the callable flow, resolved inside the account. */
  flow: string;
  /** Maximum children one execution may start through this entry. */
  max_children?: number | null;
  /** Maximum spend in USD for each child started through this entry. */
  max_usd_per_child?: number | null;
  /** Explicit opt in to a self reference; recursion is never implicit. */
  allow_self?: boolean;
}

/** A tool allowlist row as the form stores it. */
interface ToolReference {
  server_name?: string;
  tool_name?: string;
}

/**
 * True when the delegation tool is on this flow's tool allowlist.
 *
 * The allowlist section is only shown for flows that can delegate at all, so
 * the form is unchanged for the flows that never will, which is most of them.
 */
export function isDelegationToolEnabled(tools: unknown): boolean {
  if (!Array.isArray(tools)) return false;
  return tools.some((tool) => {
    const reference = (tool || {}) as ToolReference;
    return (
      reference.server_name === DELEGATION_TOOL_SERVER &&
      reference.tool_name === DELEGATION_TOOL_NAME
    );
  });
}

function readCeiling(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Read a stored `callable_flows` value into rows the form can edit.
 *
 * Null, undefined and a non-array all read back as an empty list: they are
 * the same permission, and a form that distinguished them would offer an
 * operator a difference that does not exist.
 */
export function parseCallableFlows(value: unknown): CallableFlowEntry[] {
  if (!Array.isArray(value)) return [];
  const entries: CallableFlowEntry[] = [];
  for (const item of value) {
    if (typeof item === 'string') {
      const name = item.trim();
      if (name) entries.push({ flow: name });
      continue;
    }
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const name = typeof record.flow === 'string' ? record.flow.trim() : '';
    if (!name) continue;
    entries.push({
      flow: name,
      max_children: readCeiling(record.max_children),
      max_usd_per_child: readCeiling(record.max_usd_per_child),
      allow_self: record.allow_self === true,
    });
  }
  return entries;
}

/**
 * The allowlist as it goes on the wire: unset ceilings are dropped.
 *
 * Sending `max_children: null` and omitting it mean the same thing to the
 * API, and the shorter body is the one an operator reads in a request log.
 */
export function serialiseCallableFlows(
  entries: CallableFlowEntry[]
): Array<Record<string, unknown>> {
  return entries.map((entry) => {
    const payload: Record<string, unknown> = { flow: entry.flow };
    if (entry.max_children !== null && entry.max_children !== undefined) {
      payload.max_children = entry.max_children;
    }
    if (
      entry.max_usd_per_child !== null &&
      entry.max_usd_per_child !== undefined
    ) {
      payload.max_usd_per_child = entry.max_usd_per_child;
    }
    if (entry.allow_self === true) payload.allow_self = true;
    return payload;
  });
}

/** A stable key for change detection: same list, same string. */
export function callableFlowsFingerprint(entries: CallableFlowEntry[]): string {
  return JSON.stringify(serialiseCallableFlows(entries));
}

/** Case insensitive match, the way the API resolves a reference. */
export function findCallableEntry(
  entries: CallableFlowEntry[],
  name: string
): CallableFlowEntry | undefined {
  const wanted = name.trim().toLowerCase();
  return entries.find((entry) => entry.flow.trim().toLowerCase() === wanted);
}

/**
 * Validate an allowlist the way the API will, and name the entry at fault.
 *
 * The rules mirror `CallableFlowEntry` and `validate_callable_flows` on the
 * server: no duplicates, ceilings above zero, a self reference only with the
 * explicit flag, and a list short enough for a human to review. Whether a
 * reference resolves to a flow in the account is a question only the server
 * can answer, so the form does not try.
 *
 * @param entries Rows as edited in the form.
 * @param selfName Name of the flow being edited, when it has one.
 * @returns The first problem as a sentence, or null when the list is savable.
 */
export function validateCallableFlows(
  entries: CallableFlowEntry[],
  selfName?: string | null
): string | null {
  if (entries.length > CALLABLE_FLOWS_MAX_ENTRIES) {
    return (
      `A flow may list at most ${CALLABLE_FLOWS_MAX_ENTRIES} callable flows, ` +
      `and this one lists ${entries.length}.`
    );
  }
  const seen = new Set<string>();
  const self = (selfName || '').trim().toLowerCase();
  for (const entry of entries) {
    const name = entry.flow.trim();
    if (!name) {
      return 'A callable flow entry has no flow name.';
    }
    const key = name.toLowerCase();
    if (seen.has(key)) {
      return `Callable flow '${name}' is listed twice. List each flow once, with one set of ceilings.`;
    }
    seen.add(key);
    if (self && key === self && entry.allow_self !== true) {
      return `Callable flow '${name}' is this flow itself. Select it in the row marked "this flow", which sets the recursion flag the server requires.`;
    }
    const children = entry.max_children;
    if (children !== null && children !== undefined) {
      if (!Number.isInteger(children) || children <= 0) {
        return `Callable flow '${name}': maximum children must be a whole number above zero, or blank for no limit.`;
      }
    }
    const perChild = entry.max_usd_per_child;
    if (perChild !== null && perChild !== undefined) {
      if (!Number.isFinite(perChild) || perChild <= 0) {
        return `Callable flow '${name}': maximum USD per child must be above zero, or blank for no limit.`;
      }
    }
  }
  return null;
}

/**
 * The entry a server rejection is about, read out of its message.
 *
 * Every `callable_flows` refusal the API raises quotes the entry it refused
 * ("callable_flows entry 'Child flow' does not name a flow in this account").
 * Pulling that name back out lets the form mark the row instead of printing a
 * sentence about a field the operator then has to find.
 */
export function callableFlowsErrorEntry(
  message: string | null | undefined
): string | null {
  if (!message || !message.includes('callable_flows')) return null;
  const quoted = message.match(/'([^']+)'/);
  return quoted ? quoted[1] : null;
}
