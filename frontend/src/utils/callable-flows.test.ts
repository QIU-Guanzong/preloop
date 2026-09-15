import { expect } from '@open-wc/testing';
import {
  CALLABLE_FLOWS_MAX_ENTRIES,
  DELEGATION_TOOL_NAME,
  DELEGATION_TOOL_SERVER,
  callableFlowsErrorEntry,
  callableFlowsFingerprint,
  findCallableEntry,
  isDelegationToolEnabled,
  parseCallableFlows,
  serialiseCallableFlows,
  validateCallableFlows,
} from './callable-flows';

describe('isDelegationToolEnabled', () => {
  it('is true only for the delegation tool on the preloop server', () => {
    expect(
      isDelegationToolEnabled([
        { server_name: 'preloop-mcp', tool_name: 'create_pull_request' },
        {
          server_name: DELEGATION_TOOL_SERVER,
          tool_name: DELEGATION_TOOL_NAME,
        },
      ])
    ).to.be.true;
  });

  it('is false for no tools, an empty list and a like named tool', () => {
    expect(isDelegationToolEnabled(undefined)).to.be.false;
    expect(isDelegationToolEnabled(null)).to.be.false;
    expect(isDelegationToolEnabled([])).to.be.false;
    expect(
      isDelegationToolEnabled([
        { server_name: 'other-server', tool_name: DELEGATION_TOOL_NAME },
      ])
    ).to.be.false;
  });
});

describe('parseCallableFlows', () => {
  it('reads null, undefined and a non list as the same empty permission', () => {
    expect(parseCallableFlows(null)).to.deep.equal([]);
    expect(parseCallableFlows(undefined)).to.deep.equal([]);
    expect(parseCallableFlows('run everything')).to.deep.equal([]);
    expect(parseCallableFlows([])).to.deep.equal([]);
  });

  it('keeps the ceilings and the self flag off a stored entry', () => {
    expect(
      parseCallableFlows([
        {
          flow: ' Child flow ',
          max_children: 3,
          max_usd_per_child: 1.25,
          allow_self: true,
        },
      ])
    ).to.deep.equal([
      {
        flow: 'Child flow',
        max_children: 3,
        max_usd_per_child: 1.25,
        allow_self: true,
      },
    ]);
  });

  it('drops an entry with no flow name and keeps a bare string', () => {
    expect(
      parseCallableFlows([{ max_children: 2 }, '  ', 'Child flow'])
    ).to.deep.equal([{ flow: 'Child flow' }]);
  });
});

describe('serialiseCallableFlows', () => {
  it('omits unset ceilings and an unset self flag', () => {
    expect(
      serialiseCallableFlows([
        { flow: 'Child flow', max_children: null, max_usd_per_child: null },
      ])
    ).to.deep.equal([{ flow: 'Child flow' }]);
  });

  it('sends the ceilings that are set', () => {
    expect(
      serialiseCallableFlows([
        {
          flow: 'Child flow',
          max_children: 2,
          max_usd_per_child: 0.5,
          allow_self: true,
        },
      ])
    ).to.deep.equal([
      {
        flow: 'Child flow',
        max_children: 2,
        max_usd_per_child: 0.5,
        allow_self: true,
      },
    ]);
  });
});

describe('callableFlowsFingerprint', () => {
  it('is stable for a list that did not change', () => {
    const before = callableFlowsFingerprint([
      { flow: 'Child flow', max_children: null, max_usd_per_child: null },
    ]);
    const after = callableFlowsFingerprint([{ flow: 'Child flow' }]);
    expect(after).to.equal(before);
  });

  it('changes when a ceiling changes', () => {
    const before = callableFlowsFingerprint([{ flow: 'Child flow' }]);
    const after = callableFlowsFingerprint([
      { flow: 'Child flow', max_children: 2 },
    ]);
    expect(after).to.not.equal(before);
  });
});

describe('findCallableEntry', () => {
  it('matches the way the API resolves a reference, ignoring case', () => {
    const entries = [{ flow: 'Child Flow' }];
    expect(findCallableEntry(entries, 'child flow')).to.equal(entries[0]);
    expect(findCallableEntry(entries, 'other flow')).to.be.undefined;
  });
});

describe('validateCallableFlows', () => {
  it('accepts an empty list and a list with blank ceilings', () => {
    expect(validateCallableFlows([], 'Parent flow')).to.be.null;
    expect(
      validateCallableFlows(
        [{ flow: 'Child flow', max_children: null, max_usd_per_child: null }],
        'Parent flow'
      )
    ).to.be.null;
  });

  it('names the flow listed twice', () => {
    const message = validateCallableFlows(
      [{ flow: 'Child flow' }, { flow: 'child flow' }],
      'Parent flow'
    );
    expect(message).to.be.a('string');
    expect(message).to.include("'child flow'");
  });

  it('refuses a ceiling of zero or below, naming the entry', () => {
    expect(
      validateCallableFlows([{ flow: 'Child flow', max_children: 0 }], 'Parent')
    ).to.include("'Child flow'");
    expect(
      validateCallableFlows(
        [{ flow: 'Child flow', max_usd_per_child: -1 }],
        'Parent'
      )
    ).to.include("'Child flow'");
  });

  it('refuses a fractional child count', () => {
    expect(
      validateCallableFlows(
        [{ flow: 'Child flow', max_children: 1.5 }],
        'Parent'
      )
    ).to.include('whole number');
  });

  it('refuses a self reference without the flag and accepts it with one', () => {
    expect(
      validateCallableFlows([{ flow: 'Parent flow' }], 'Parent flow')
    ).to.include('this flow itself');
    expect(
      validateCallableFlows(
        [{ flow: 'Parent flow', allow_self: true }],
        'Parent flow'
      )
    ).to.be.null;
  });

  it('refuses a list too long for a human to review', () => {
    const entries = Array.from(
      { length: CALLABLE_FLOWS_MAX_ENTRIES + 1 },
      (_, index) => ({ flow: `Flow ${index}` })
    );
    expect(validateCallableFlows(entries, 'Parent')).to.include(
      String(CALLABLE_FLOWS_MAX_ENTRIES)
    );
  });
});

describe('callableFlowsErrorEntry', () => {
  it('reads the entry out of a server refusal', () => {
    expect(
      callableFlowsErrorEntry(
        "callable_flows entry 'Child flow' does not name a flow in this account"
      )
    ).to.equal('Child flow');
    expect(
      callableFlowsErrorEntry(
        "callable_flows entry 'Parent flow' is this flow itself; set allow_self on the entry to allow recursion"
      )
    ).to.equal('Parent flow');
  });

  it('ignores an error about anything else', () => {
    expect(callableFlowsErrorEntry(null)).to.be.null;
    expect(callableFlowsErrorEntry('Flow name is required.')).to.be.null;
    expect(callableFlowsErrorEntry("Model 'gpt' is unknown")).to.be.null;
  });
});
