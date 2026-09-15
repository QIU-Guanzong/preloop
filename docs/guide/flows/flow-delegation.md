# Delegation: one flow running another with `run_flow`

A flow execution can start another flow of the same account as a child of
itself. The tool is `run_flow`, it is off by default, and every rule that
decides whether a call is permitted runs on the server.

Delegation is asynchronous today. `run_flow` returns as soon as the child
execution row exists and never blocks the calling turn. A parent that needs
an outcome polls the child with `get_execution`; waiting for one, instead of
polling, is a separate change.

## Turning it on for a flow

Two independent switches, both on the calling flow:

1. `run_flow` in the flow's `allowed_mcp_tools`. The tool is default off, so
   an agent that has not been given it does not see it in its tool list and
   cannot call it by name either.
2. The target flow in the flow's `callable_flows` allowlist. Selecting the
   tool grants delegation in general; the allowlist decides what may be run.
   An entry may cap how many children of that flow one execution may start.

Neither switch implies the other. A flow with the tool and an empty
allowlist can call nothing; a flow with an allowlist and no tool cannot
delegate at all.

## What the agent passes

| Argument | Meaning |
| --- | --- |
| `flow` | Slug or name of the target, resolved inside the calling account |
| `payload` | Trigger payload for the child, readable as `{{trigger_event.payload.<key>}}` |
| `label` | Short label recorded on the child so siblings are distinguishable |
| `timeout_seconds` | Window for the child, clamped to the caller's own remaining time |

Model and harness overrides inside `payload` are stripped: a child runs on
its own flow's routing, never on routing chosen by the calling agent.

## What comes back

One task record, in the delegation shapes frozen in
`docs/guide/flow-delegation-shapes.md`: the child execution id, the root of
the delegation tree as `contextId`, the state, and the depth. There are no
artifacts on a child that has not run and no cost on one that has spent
nothing.

A refusal is a record too, not an exception: state `TASK_STATE_REJECTED`
with `preloop.ai/refusalReason` naming the rule that declined the call. The
agent reads a reason instead of parsing prose.

## The rules, in the order they run

| Reason | The rule that declined |
| --- | --- |
| `tool_not_allowed` | `run_flow` is not on the calling flow's tool allowlist |
| `flow_not_found` | The reference names no flow in the calling account |
| `flow_not_callable` | The flow is disabled, or is not on the caller's `callable_flows` |
| `depth_exceeded` | The child would sit deeper than the configured maximum depth |
| `cycle_detected` | The target is already running above this execution |
| `fanout_exceeded` | This execution has reached its cap on direct children |

Each check fails closed and each refusal is audited with the correlation id
of the call that was refused, so a refused delegation is visible next to the
executions it did not create.

The tool allowlist is read from the flow row at call time rather than from
the credentials the execution is holding, so revoking `run_flow` from a flow
takes effect on runs that are already in progress.

Two controls are not in this list because they are enforced before the call
reaches delegation at all: the account kill switch, which halts every tool
call while the `tools` scope is halted and every new execution while the
`flows` scope is, and the central access policy, whose deny stops a call
that the allowlist would otherwise have permitted. A delegated start is
refused by the halt exactly as a manual start is.

## Reading a child with `get_execution`

`get_execution` is the other half of the pair and the same two switches do
not apply to it: it needs only to be in the flow's `allowed_mcp_tools`, and
it is off by default like `run_flow`. There is no allowlist for reads,
because the scope is not configurable.

| Argument | Meaning |
| --- | --- |
| `execution_id` | The execution to read, as returned by `run_flow` |
| `include_result` | Whether to return the result payload. Default false |

### What it may read

This execution, and the executions below it: a child, a grandchild, anything
whose `parent_execution_id` chain reaches the caller. Nothing else, and the
scope is decided on the server from the calling execution's own identity, not
from an argument.

Everything out of scope is refused with one reason, `execution_not_found`,
and one message: a sibling, an unrelated execution of the same account, an
execution of another account and an id nobody ever issued are answered
identically. A refusal that distinguished them would answer "does this
execution exist?" for rows the caller cannot read, one guessed id at a time.

### What comes back

The same task record `run_flow` returns, filled in with what the execution
has actually done: its state and the Preloop status that state was mapped
from, its depth, the cost and tokens spent so far, and its result as an
artifact when it has finished and `include_result` was set. An execution that
is still running hands over no artifact, whatever the flag says: there is no
result until there is.

A terminal execution that failed carries its failure category on the status
message, under `preloop.ai/failureCategory`, next to the human readable error
text. That category is the coarse, closed vocabulary the rest of Preloop
groups failures by, so a parent can branch on it instead of matching on an
error string.

A result larger than `FLOW_DELEGATION_RESULT_MAX_BYTES` comes back truncated
rather than whole: the artifact carries the serialised document cut to the
cap, says so in its description, flags it with `preloop.ai/truncated`, and
points at `GET /flows/executions/{id}/result`, which still serves the whole
document. One tool call cannot fill the caller's context window with a
child's output.

Every call writes one audit row, permitted or refused, carrying the calling
execution and the correlation id of the call.

## Limits an operator can set

| Setting | Default | Meaning |
| --- | --- | --- |
| `FLOW_DELEGATION_MAX_DEPTH` | `2` | Deepest a delegation tree may grow. A root run is depth 0, its child 1, its grandchild 2, so the default refuses a great grandchild. `0` disables delegation on the instance. |
| `FLOW_DELEGATION_MAX_CHILDREN` | `25` | How many direct children one execution may start. Defaults to the matrix fan out ceiling, so one execution cannot start more work by delegating than by matrixing. |
| `FLOW_DELEGATION_RESULT_MAX_BYTES` | `16384` | Largest result payload `get_execution` returns whole. A larger result is truncated, flagged and pointed at. |

An allowlist entry's own `max_children` is applied on top of the instance
ceiling: whichever refuses first, refuses.

## Reading the tree

Every delegated child carries its parent execution, the root of its tree,
and its depth, so a tree can be reconstructed from the execution rows alone
without a log. A child of a child keeps the root of the whole tree, not its
immediate parent.
