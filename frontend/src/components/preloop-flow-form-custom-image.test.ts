import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import { runnerSelectionKind } from '../utils/runner-pool';

const OFFICE_MAC = 'office-mac';
const CANONICAL = 'registry.example.com/team/project:release';
const LEGACY = 'registry.example.com/team/legacy:release';

const apiState: {
  runners: unknown[];
  account: Record<string, unknown> | null;
  agents: unknown[];
} = {
  runners: [],
  account: null,
  agents: [],
};

describe('runnerSelectionKind', () => {
  it('separates private tokens from hosted and auto', () => {
    expect(runnerSelectionKind('')).to.equal('auto');
    expect(runnerSelectionKind(null)).to.equal('auto');
    expect(runnerSelectionKind('  ')).to.equal('auto');
    expect(runnerSelectionKind('auto')).to.equal('auto');
    expect(runnerSelectionKind('AUTO')).to.equal('auto');
    expect(runnerSelectionKind('server')).to.equal('hosted');
    expect(runnerSelectionKind(OFFICE_MAC)).to.equal('private');
    expect(runnerSelectionKind('gpu')).to.equal('private');
  });
});

describe('PreloopFlowForm custom container image', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sessionStorage.clear();
    apiState.runners = [
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: OFFICE_MAC,
        labels: ['local'],
        status: 'online',
      },
    ];
    apiState.account = {
      id: 'acct-1',
      organization_name: 'Example Org',
      default_runner_pool: null,
      hosted_minutes_remaining: null,
      created_at: '2026-09-04T00:00:00Z',
      updated_at: '2026-09-04T00:00:00Z',
    };
    apiState.agents = [];
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/runners')) {
        return new Response(JSON.stringify(apiState.runners));
      }
      if (target.includes('/api/v1/account/details')) {
        return new Response(JSON.stringify(apiState.account));
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: apiState.agents }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (flow: Record<string, unknown>) => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const submit = async (element: PreloopFlowForm) => {
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    return event.detail.flow;
  };

  const imageInput = (element: PreloopFlowForm) =>
    element.shadowRoot?.querySelector(
      'sl-input[label="Custom container image"]'
    ) as SlInput | null;

  const unavailableText = (element: PreloopFlowForm) =>
    element.shadowRoot?.querySelector('[data-custom-image-unavailable]')
      ?.textContent || '';

  const setRunnerPool = async (element: PreloopFlowForm, value: string) => {
    (element as any).handleRunnerPoolChange({ detail: { value } });
    await element.updateComplete;
  };

  const typeImage = async (element: PreloopFlowForm, value: string) => {
    const input = imageInput(element);
    expect(input, 'image input is rendered').to.exist;
    input!.value = value;
    input!.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
  };

  const flow = (extra: Record<string, unknown>) => ({
    name: 'Review',
    prompt_template: 'review',
    agent_type: 'codex',
    runner_pool: OFFICE_MAC,
    ...extra,
  });

  it('offers no override for a new blank flow that resolves to Auto', async () => {
    const element = await mount({
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
    });
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('Auto (private first');
    const agentConfig = (await submit(element)).agent_config;
    expect('image' in agentConfig).to.equal(false);
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('keeps a canonical image for a private runner', async () => {
    const element = await mount(flow({ agent_config: { image: CANONICAL } }));
    expect(imageInput(element)?.value).to.equal(CANONICAL);
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('canonicalizes an unchanged legacy alias while it is visible', async () => {
    const element = await mount(
      flow({ agent_config: { docker_image: LEGACY } })
    );
    expect(imageInput(element)?.value).to.equal(LEGACY);
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(LEGACY);
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('prefers the canonical alias and drops the legacy one', async () => {
    const element = await mount(
      flow({ agent_config: { image: CANONICAL, docker_image: LEGACY } })
    );
    expect(imageInput(element)?.value).to.equal(CANONICAL);
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('writes a typed override trimmed and drops the legacy alias', async () => {
    const element = await mount(
      flow({
        agent_config: { docker_image: LEGACY, custom_option: { keep: true } },
      })
    );
    await typeImage(element, '  registry.example.com/team/new:1  ');
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal('registry.example.com/team/new:1');
    expect('docker_image' in agentConfig).to.equal(false);
    expect(agentConfig.custom_option).to.deep.equal({ keep: true });
  });

  it('removes both keys on an explicit clear', async () => {
    const element = await mount(
      flow({ agent_config: { image: CANONICAL, docker_image: LEGACY } })
    );
    await typeImage(element, '');
    const agentConfig = (await submit(element)).agent_config;
    expect('image' in agentConfig).to.equal(false);
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('uses the account default when the flow inherits a private runner', async () => {
    apiState.account = { ...apiState.account, default_runner_pool: OFFICE_MAC };
    const element = await mount(
      flow({ runner_pool: undefined, agent_config: { image: CANONICAL } })
    );
    expect(imageInput(element)).to.exist;
    const help = element.shadowRoot
      ?.querySelector('sl-input[label="Custom container image"]')
      ?.getAttribute('help-text');
    expect(help).to.contain(OFFICE_MAC);
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
  });

  it('hides the editor and preserves aliases for an Auto override', async () => {
    const saved = { image: CANONICAL, docker_image: LEGACY };
    const element = await mount(
      flow({ runner_pool: 'auto', agent_config: saved })
    );
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('Auto (private first');
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
    expect(agentConfig.docker_image).to.equal(LEGACY);
    expect(saved.image).to.equal(CANONICAL);
  });

  it('hides the editor and preserves aliases for hosted execution', async () => {
    const saved = { image: CANONICAL, docker_image: LEGACY };
    const element = await mount(
      flow({ runner_pool: 'server', agent_config: saved })
    );
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('Preloop hosted');
    expect(unavailableText(element)).to.contain('is kept');
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig).to.deep.equal(saved);
  });

  it('hides the editor when the account default cannot promise private', async () => {
    apiState.account = { ...apiState.account, default_runner_pool: 'auto' };
    const saved = { image: CANONICAL };
    const element = await mount(
      flow({ runner_pool: undefined, agent_config: saved })
    );
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('account default is Auto');
    expect((await submit(element)).agent_config).to.deep.equal(saved);
  });

  it('preserves aliases for native host execution profiles', async () => {
    const saved = { image: CANONICAL, docker_image: LEGACY };
    const element = await mount({
      name: 'Ask locally',
      prompt_template: 'summarize',
      agent_type: 'cursor',
      runner_pool: OFFICE_MAC,
      agent_config: { ...saved, host_exec_profile: 'cursor-ask' },
    });
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('Host execution profiles');
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
    expect(agentConfig.docker_image).to.equal(LEGACY);
    expect(agentConfig.host_exec_profile).to.equal('cursor-ask');
  });

  it('preserves aliases while persistent execution is selected', async () => {
    apiState.agents = [{ id: 'agent-1', name: 'Team agent' }];
    const saved = { image: CANONICAL, docker_image: LEGACY };
    const element = await mount({
      ...flow({ agent_config: saved }),
      id: 'flow-1',
    });
    (element as any).longRunningAgents = apiState.agents;
    (element as any).flowExecutionPath = 'persistent';
    (element as any).targetAgentId = 'agent-1';
    await element.updateComplete;
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('Persistent agents');
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal(CANONICAL);
    expect(agentConfig.docker_image).to.equal(LEGACY);
    expect(agentConfig.execution_path).to.equal('persistent');
  });

  it('round-trips an override identically with or without long-running agents', async () => {
    const withoutAgents = await mount(
      flow({ agent_config: { docker_image: LEGACY } })
    );
    const withoutPayload = await submit(withoutAgents);

    apiState.agents = [{ id: 'agent-1', name: 'Team agent' }];
    const withAgents = await mount(
      flow({ agent_config: { docker_image: LEGACY } })
    );
    expect(imageInput(withAgents)?.value).to.equal(LEGACY);
    const withPayload = await submit(withAgents);

    expect(withPayload.agent_config.image).to.equal(LEGACY);
    expect('docker_image' in withPayload.agent_config).to.equal(false);
    expect(withPayload.agent_config.execution_path).to.equal('ephemeral');
    expect(withoutPayload.agent_config.image).to.equal(
      withPayload.agent_config.image
    );
  });

  it('keeps a typed override when the selection switches away and back', async () => {
    const element = await mount(flow({ agent_config: { image: CANONICAL } }));
    await typeImage(element, 'registry.example.com/team/new:2');

    await setRunnerPool(element, 'server');
    expect(imageInput(element)).to.be.null;
    expect(unavailableText(element)).to.contain('is kept');

    await setRunnerPool(element, OFFICE_MAC);
    expect(imageInput(element)?.value).to.equal(
      'registry.example.com/team/new:2'
    );

    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig.image).to.equal('registry.example.com/team/new:2');
    expect('docker_image' in agentConfig).to.equal(false);
  });

  it('keeps a typed image draft across a GitHub OAuth round-trip', async () => {
    const typed = 'registry.example.com/team/oauth-draft:1';
    const element = await mount(flow({ agent_config: { image: CANONICAL } }));
    await typeImage(element, typed);

    element.dispatchEvent(new CustomEvent('github-oauth-starting'));
    const snapshot = JSON.parse(
      sessionStorage.getItem('preloop_flow_form_state') || '{}'
    );
    expect(snapshot.customImage).to.equal(typed);
    expect(snapshot.flow?.agent_config?.image).to.equal(CANONICAL);

    fixtureCleanup();
    const restored = await mount(flow({ agent_config: { image: CANONICAL } }));
    expect(imageInput(restored)?.value).to.equal(typed);
    const agentConfig = (await submit(restored)).agent_config;
    expect(agentConfig.image).to.equal(typed);
  });

  it('keeps an explicit clear across a GitHub OAuth round-trip', async () => {
    const element = await mount(flow({ agent_config: { image: CANONICAL } }));
    await typeImage(element, '');

    element.dispatchEvent(new CustomEvent('github-oauth-starting'));
    fixtureCleanup();
    const restored = await mount(flow({ agent_config: { image: CANONICAL } }));
    expect(imageInput(restored)?.value).to.equal('');
    const agentConfig = (await submit(restored)).agent_config;
    expect('image' in agentConfig).to.equal(false);
  });

  it('does not rewrite aliases when a hidden field is saved unchanged', async () => {
    const saved = {
      image: CANONICAL,
      docker_image: LEGACY,
      environment_profile: 'team-tests',
      custom_option: { keep: true },
    };
    const element = await mount(
      flow({ runner_pool: 'server', agent_config: saved })
    );
    const agentConfig = (await submit(element)).agent_config;
    expect(agentConfig).to.deep.equal(saved);
    expect(saved.docker_image).to.equal(LEGACY);
  });
});
