import { useState } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LLMChannelEditor } from '../LLMChannelEditor';

const {
  update,
  testLLMChannel,
  discoverLLMChannelModels,
} = vi.hoisted(() => ({
  update: vi.fn(),
  testLLMChannel: vi.fn(),
  discoverLLMChannelModels: vi.fn(),
}));

vi.mock('../../../api/systemConfig', () => ({
  systemConfigApi: {
    update: (...args: unknown[]) => update(...args),
    testLLMChannel: (...args: unknown[]) => testLLMChannel(...args),
    discoverLLMChannelModels: (...args: unknown[]) => discoverLLMChannelModels(...args),
  },
}));

describe('LLMChannelEditor', () => {
  beforeEach(() => {
    update.mockReset();
    testLLMChannel.mockReset();
    discoverLLMChannelModels.mockReset();
  });

  function selectOptionValues(label: string): string[] {
    const select = screen.getByLabelText(label) as HTMLSelectElement;
    return Array.from(select.options).map((option) => option.value);
  }

  const openAiItems = [
    { key: 'LLM_CHANNELS', value: 'openai' },
    { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
    { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
    { key: 'LLM_OPENAI_ENABLED', value: 'true' },
    { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
    { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' },
    { key: 'LITELLM_MODEL', value: 'openai/gpt-4o-mini' },
  ];

  function lastDraftCall(onDraftItemsChange: ReturnType<typeof vi.fn>) {
    const calls = onDraftItemsChange.mock.calls;
    return calls[calls.length - 1]?.[0] || [];
  }

  it('reports an empty generation backend draft when channel settings are unchanged', async () => {
    const onDraftItemsChange = vi.fn();
    const { rerender } = render(
      <LLMChannelEditor
        items={openAiItems}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
        onDraftItemsChange={onDraftItemsChange}
      />
    );

    await waitFor(() => expect(onDraftItemsChange).toHaveBeenCalledWith([]));
    expect(onDraftItemsChange).toHaveBeenCalledTimes(1);

    rerender(
      <LLMChannelEditor
        items={openAiItems}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
        onDraftItemsChange={onDraftItemsChange}
      />
    );

    expect(onDraftItemsChange).toHaveBeenCalledTimes(1);
  });

  it('reports unsaved channel edits as generation backend draft items', async () => {
    const onDraftItemsChange = vi.fn();
    render(
      <LLMChannelEditor
        items={openAiItems}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
        onDraftItemsChange={onDraftItemsChange}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.change(await screen.findByLabelText('Base URL'), {
      target: { value: 'https://proxy.example.com/v1' },
    });
    fireEvent.change(screen.getByLabelText('API Key'), {
      target: { value: 'sk-draft' },
    });
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'gpt-4o-mini,gpt-4o' },
    });

    await waitFor(() => {
      const draft = lastDraftCall(onDraftItemsChange);
      expect(draft).toContainEqual({ key: 'LLM_OPENAI_BASE_URL', value: 'https://proxy.example.com/v1' });
      expect(draft).toContainEqual({ key: 'LLM_OPENAI_API_KEY', value: 'sk-draft' });
      expect(draft).toContainEqual({ key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini,gpt-4o' });
    });
  });

  it('loads and tests a multi-model Responses API channel without changing public model names', async () => {
    testLLMChannel.mockResolvedValue({ success: true });
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'anspire' },
          { key: 'LLM_ANSPIRE_PROTOCOL', value: 'openai' },
          { key: 'LLM_ANSPIRE_API_SURFACE', value: 'responses' },
          { key: 'LLM_ANSPIRE_BASE_URL', value: 'https://open-gateway.anspire.cn/v6' },
          { key: 'LLM_ANSPIRE_ENABLED', value: 'true' },
          { key: 'LLM_ANSPIRE_API_KEY', value: 'sk-test' },
          { key: 'LLM_ANSPIRE_MODELS', value: 'gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Anspire Open/i }));
    expect(await screen.findByLabelText('API Surface')).toHaveValue('responses');
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    await waitFor(() => expect(testLLMChannel).toHaveBeenCalledWith(expect.objectContaining({
      apiSurface: 'responses',
      models: ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],
    })));
  });

  it.each(['response', 'responses_api', 'responses-api'])(
    'canonicalizes the saved %s API-surface alias before an unrelated save',
    async (apiSurfaceAlias) => {
      update.mockResolvedValue({
        success: true,
        configVersion: 'v2',
        appliedCount: 1,
        skippedMaskedCount: 0,
        reloadTriggered: true,
        updatedKeys: ['LLM_OPENAI_API_SURFACE', 'LLM_OPENAI_BASE_URL'],
        warnings: [],
      });

      render(
        <LLMChannelEditor
          items={[
            ...openAiItems,
            { key: 'LLM_OPENAI_API_SURFACE', value: apiSurfaceAlias },
          ]}
          configVersion="v1"
          maskToken="******"
          onSaved={() => {}}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /OpenAI/i }));
      expect(await screen.findByLabelText('API Surface')).toHaveValue('responses');
      fireEvent.change(screen.getByLabelText('Base URL'), {
        target: { value: 'https://proxy.example.com/v1' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

      await waitFor(() => expect(update).toHaveBeenCalled());
      const updateItemMap = new Map(
        update.mock.calls[0][0].items.map((item: { key: string; value: string }) => [item.key, item.value]),
      );
      expect(updateItemMap.get('LLM_OPENAI_API_SURFACE')).toBe('responses');
    },
  );

  it('preserves an invalid saved API surface during an unrelated save', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_OPENAI_BASE_URL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          ...openAiItems,
          { key: 'LLM_OPENAI_API_SURFACE', value: 'respones' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI/i }));
    expect(await screen.findByLabelText('API Surface')).toHaveValue('respones');
    expect(screen.getByRole('option', { name: 'Invalid configuration: respones' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://proxy.example.com/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    const updateItemMap = new Map(
      update.mock.calls[0][0].items.map((item: { key: string; value: string }) => [item.key, item.value]),
    );
    expect(updateItemMap.get('LLM_OPENAI_API_SURFACE')).toBe('respones');
  });

  it('allows an invalid Hermes API surface to be repaired to chat completions', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_API_SURFACE', value: 'responses' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: 'sk-hermes-test-value' },
          { key: 'LLM_HERMES_MODELS', value: 'hermes-agent' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Hermes/i }));
    const apiSurface = await screen.findByLabelText('API Surface');
    expect(apiSurface).toHaveValue('responses');
    expect(apiSurface).toBeEnabled();

    fireEvent.change(apiSurface, { target: { value: 'chat_completions' } });
    expect(apiSurface).toHaveValue('chat_completions');
  });

  it('returns to an empty generation backend draft after channel edits are restored', async () => {
    const onDraftItemsChange = vi.fn();
    render(
      <LLMChannelEditor
        items={openAiItems}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
        onDraftItemsChange={onDraftItemsChange}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    const baseUrlInput = await screen.findByLabelText('Base URL');
    fireEvent.change(baseUrlInput, { target: { value: 'https://proxy.example.com/v1' } });
    await waitFor(() => expect(lastDraftCall(onDraftItemsChange)).toContainEqual({
      key: 'LLM_OPENAI_BASE_URL',
      value: 'https://proxy.example.com/v1',
    }));

    fireEvent.change(baseUrlInput, { target: { value: 'https://api.openai.com/v1' } });

    await waitFor(() => {
      expect(lastDraftCall(onDraftItemsChange)).toEqual([]);
    });
  });

  it('does not emit invalid channel env keys while the channel name is empty', async () => {
    const onDraftItemsChange = vi.fn();
    render(
      <LLMChannelEditor
        items={openAiItems}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
        onDraftItemsChange={onDraftItemsChange}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.change(await screen.findByLabelText('Channel name'), { target: { value: '' } });

    await waitFor(() => {
      expect(lastDraftCall(onDraftItemsChange)).toEqual([]);
    });
    expect(onDraftItemsChange.mock.calls.flatMap((call) => call[0]).some((item) => item.key.startsWith('LLM__'))).toBe(false);
  });

  it('renders API Key input with controlled visibility', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
          { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));

    const input = await screen.findByLabelText('API Key');
    expect(input).toHaveAttribute('type', 'password');

    fireEvent.click(screen.getByRole('button', { name: 'Show content' }));
    expect(input).toHaveAttribute('type', 'text');
  });

  it('shows help dialogs for channel editor fields', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-v4-flash' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'View Base URL configuration help' }));

    expect(screen.getByRole('dialog', { name: 'Base URL' })).toBeInTheDocument();
    expect(screen.getByText('Endpoint root for this channel.')).toBeInTheDocument();
    expect(screen.getByText('LLM_DEEPSEEK_BASE_URL=https://api.deepseek.com')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(await screen.findByRole('button', { name: 'View Temperature configuration help' }));

    expect(screen.getByRole('dialog', { name: 'Temperature' })).toBeInTheDocument();
    expect(screen.getByText('Unified runtime sampling temperature.')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(await screen.findByRole('button', { name: 'View Runtime capability checks (optional) configuration help' }));

    expect(screen.getByRole('dialog', { name: 'Runtime Capability Checks' })).toBeInTheDocument();
    expect(screen.getByText('Manually checks JSON, tools, stream, or vision support for the current channel model.')).toBeInTheDocument();
  });

  it('hides LiteLLM wording when advanced YAML routing is enabled', () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LITELLM_CONFIG', value: './litellm_config.yaml' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
          { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    expect(screen.getByText(/An advanced model routing YAML is configured/i)).toBeInTheDocument();
    expect(screen.getByText(/The runtime primary model \/ fallback models \/ Vision \/ Temperature are still determined by the general fields below/i)).toBeInTheDocument();
    expect(screen.queryByText(/LiteLLM/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/LITELLM_CONFIG/i)).not.toBeInTheDocument();
  });

  it('excludes Hermes-only route from Agent and Vision runtime selects', () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_BASE_URL', value: 'http://127.0.0.1:8642/v1' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: '******' },
          { key: 'LLM_HERMES_MODELS', value: 'hermes-agent' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    expect(selectOptionValues('Primary model')).toContain('openai/hermes-agent');
    expect(selectOptionValues('Agent primary model')).not.toContain('openai/hermes-agent');
    expect(selectOptionValues('Vision model')).not.toContain('openai/hermes-agent');
  });

  it('keeps mixed Hermes route for Agent but excludes it from Vision', () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes,remote,pure' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_BASE_URL', value: 'http://127.0.0.1:8642/v1' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: '******' },
          { key: 'LLM_HERMES_MODELS', value: 'shared-route' },
          { key: 'LLM_REMOTE_PROTOCOL', value: 'openai' },
          { key: 'LLM_REMOTE_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_REMOTE_ENABLED', value: 'true' },
          { key: 'LLM_REMOTE_API_KEY', value: 'sk-remote' },
          { key: 'LLM_REMOTE_MODELS', value: 'shared-route' },
          { key: 'LLM_PURE_PROTOCOL', value: 'openai' },
          { key: 'LLM_PURE_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_PURE_ENABLED', value: 'true' },
          { key: 'LLM_PURE_API_KEY', value: 'sk-pure' },
          { key: 'LLM_PURE_MODELS', value: 'pure-route' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    expect(selectOptionValues('Primary model')).not.toContain('openai/shared-route');
    expect(selectOptionValues('Primary model')).toContain('openai/pure-route');
    expect(selectOptionValues('Agent primary model')).toContain('openai/shared-route');
    expect(selectOptionValues('Vision model')).not.toContain('openai/shared-route');
  });

  it('rejects bare mixed Hermes route before saving runtime generation config', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes,remote' },
          { key: 'LITELLM_MODEL', value: 'shared-route' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_BASE_URL', value: 'http://127.0.0.1:8642/v1' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: 'sk-hermes' },
          { key: 'LLM_HERMES_MODELS', value: 'shared-route' },
          { key: 'LLM_REMOTE_PROTOCOL', value: 'openai' },
          { key: 'LLM_REMOTE_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_REMOTE_ENABLED', value: 'true' },
          { key: 'LLM_REMOTE_API_KEY', value: 'sk-remote' },
          { key: 'LLM_REMOTE_MODELS', value: 'shared-route' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('slider'), { target: { value: '0.2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    expect(await screen.findByText(/A mixed Hermes\/non-Hermes route is not supported as the primary or fallback model/i)).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it('does not test runtime-only masked Hermes secrets from the settings UI', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_BASE_URL', value: 'http://127.0.0.1:8642/v1' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: '******', rawValueExists: false },
          { key: 'LLM_HERMES_MODELS', value: 'hermes-agent' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Hermes/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));
    fireEvent.click(screen.getByLabelText('JSON'));
    fireEvent.click(screen.getByRole('button', { name: 'Check capabilities' }));

    const messages = await screen.findAllByText(/A runtime-injected Hermes Key is not sent back to the browser/i);
    expect(messages.length).toBeGreaterThanOrEqual(3);
    expect(testLLMChannel).not.toHaveBeenCalled();
    expect(discoverLLMChannelModels).not.toHaveBeenCalled();
  });

  it('keeps pure non-Hermes route in Agent and Vision runtime selects', () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'remote' },
          { key: 'LLM_REMOTE_PROTOCOL', value: 'openai' },
          { key: 'LLM_REMOTE_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_REMOTE_ENABLED', value: 'true' },
          { key: 'LLM_REMOTE_API_KEY', value: 'sk-remote' },
          { key: 'LLM_REMOTE_MODELS', value: 'gpt-4o-mini' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    expect(selectOptionValues('Primary model')).toContain('openai/gpt-4o-mini');
    expect(selectOptionValues('Agent primary model')).toContain('openai/gpt-4o-mini');
    expect(selectOptionValues('Vision model')).toContain('openai/gpt-4o-mini');
  });

  it('keeps minimax-prefixed models in runtime selections', () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
          { key: 'LLM_OPENAI_MODELS', value: 'minimax/MiniMax-M1' },
        ]}
        configVersion="v1"
        maskToken="******"
        modelProviderPrefixes={['minimax', 'openai']}
        onSaved={() => {}}
      />
    );

    const primaryModelSelect = screen.getByRole('combobox', { name: 'Primary model' });
    const agentModelSelect = screen.getByRole('combobox', { name: 'Agent primary model' });
    const visionModelSelect = screen.getByRole('combobox', { name: 'Vision model' });

    expect(within(primaryModelSelect).getByRole('option', { name: 'minimax/MiniMax-M1' })).toBeInTheDocument();
    expect(within(agentModelSelect).getByRole('option', { name: 'minimax/MiniMax-M1' })).toBeInTheDocument();
    expect(within(visionModelSelect).getByRole('option', { name: 'minimax/MiniMax-M1' })).toBeInTheDocument();
  });

  it('uses DeepSeek V4 defaults when adding the official preset', async () => {
    render(
      <LLMChannelEditor
        items={[]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));

    await screen.findByRole('button', { name: /DeepSeek \(official\)/i });
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://api.deepseek.com');
    expect(screen.getByLabelText('Models (comma-separated)')).toHaveValue('deepseek-v4-flash,deepseek-v4-pro');
  });

  it.each([
    ['minimax', /MiniMax \(official\)/i, 'https://api.minimax.io/v1', 'MiniMax-M3,MiniMax-M2.7,MiniMax-M2.7-highspeed'],
    ['volcengine', /Volcengine Ark/i, 'https://ark.cn-beijing.volces.com/api/v3', 'doubao-seed-1-6-251015,doubao-seed-1-6-thinking-251015'],
  ])('uses %s OpenAI-compatible defaults when adding the official preset', async (preset, buttonName, baseUrl, models) => {
    render(
      <LLMChannelEditor
        items={[]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: preset } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));

    await screen.findByRole('button', { name: buttonName });
    expect(screen.getAllByRole('combobox').some((select) => (
      select instanceof HTMLSelectElement && select.value === 'openai'
    ))).toBe(true);
    expect(screen.getByLabelText('Base URL')).toHaveValue(baseUrl);
    expect(screen.getByLabelText('Models (comma-separated)')).toHaveValue(models);
  });

  it('shows provider capability badges, official sources, and config hints', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openrouter' },
          { key: 'LLM_OPENROUTER_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENROUTER_BASE_URL', value: 'https://openrouter.ai/api/v1' },
          { key: 'LLM_OPENROUTER_ENABLED', value: 'true' },
          { key: 'LLM_OPENROUTER_API_KEY', value: 'sk-or-test' },
          { key: 'LLM_OPENROUTER_MODELS', value: '~anthropic/claude-sonnet-latest' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenRouter/i }));

    expect(await screen.findByText('Configuration reference')).toBeInTheDocument();
    expect(screen.getByText('OpenAI compatible')).toBeInTheDocument();
    expect(screen.getByText('Aggregator')).toBeInTheDocument();
    expect(screen.getByText('Model discovery')).toBeInTheDocument();
    expect(screen.getByText(/The model list and visibility depend on account permissions and the API Key/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'OpenRouter Models API' })).toHaveAttribute(
      'href',
      'https://openrouter.ai/docs/api/api-reference/models/get-models',
    );
    expect(screen.getByText(/Capability tags are for configuration reference only; they do not mean the runtime capability has been verified/i)).toBeInTheDocument();
  });

  it('shows model-discovery capability for SiliconFlow provider hints', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'siliconflow' },
          { key: 'LLM_SILICONFLOW_PROTOCOL', value: 'openai' },
          { key: 'LLM_SILICONFLOW_BASE_URL', value: 'https://api.siliconflow.cn/v1' },
          { key: 'LLM_SILICONFLOW_ENABLED', value: 'true' },
          { key: 'LLM_SILICONFLOW_API_KEY', value: 'sk-test' },
          { key: 'LLM_SILICONFLOW_MODELS', value: 'deepseek-ai/DeepSeek-V3.2' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /SiliconFlow/i }));

    expect(await screen.findByText('Model discovery')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'SiliconFlow Models' })).toBeInTheDocument();
  });

  it('does not show provider metadata for custom or unknown channels', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy' },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai' },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1' },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true' },
          { key: 'LLM_MY_PROXY_API_KEY', value: 'sk-test' },
          { key: 'LLM_MY_PROXY_MODELS', value: 'custom-model' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));

    expect(screen.queryByText('Configuration reference')).not.toBeInTheDocument();
    expect(screen.queryByText(/Official sources/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Capability tags are for configuration reference only/i)).not.toBeInTheDocument();
  });

  it('preserves manually edited base URL and models when switching preset names', async () => {
    render(
      <LLMChannelEditor
        items={[]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));

    await screen.findByRole('button', { name: /DeepSeek \(official\)/i });
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://proxy.example.com/v1' },
    });
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'custom-model-a,custom-model-b' },
    });
    fireEvent.change(screen.getByLabelText('Channel name'), {
      target: { value: 'minimax' },
    });

    await screen.findByRole('button', { name: /MiniMax \(official\)/i });
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://proxy.example.com/v1');
    expect(screen.getByLabelText('Models (comma-separated)')).toHaveValue('custom-model-a,custom-model-b');
  });

  it('uses the selected preset defaults when adding a duplicate provider channel', async () => {
    render(
      <LLMChannelEditor
        items={[]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'minimax' } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));
    await screen.findByRole('button', { name: /MiniMax \(official\)/i });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));

    await screen.findByRole('button', { name: /minimax2/i });
    expect(screen.getAllByLabelText('Channel name').map((input) => (input as HTMLInputElement).value)).toEqual([
      'minimax',
      'minimax2',
    ]);
    expect(screen.getAllByLabelText('Base URL').map((input) => (input as HTMLInputElement).value)).toEqual([
      'https://api.minimax.io/v1',
      'https://api.minimax.io/v1',
    ]);
    expect(screen.getAllByLabelText('Models (comma-separated)').map((input) => (input as HTMLInputElement).value)).toEqual([
      'MiniMax-M3,MiniMax-M2.7,MiniMax-M2.7-highspeed',
      'MiniMax-M3,MiniMax-M2.7,MiniMax-M2.7-highspeed',
    ]);
    expect(screen.getAllByRole('link', { name: 'MiniMax OpenAI API' })).toHaveLength(1);
  });

  it('saves the MiniMax preset into LLM channel env keys', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_CHANNELS', 'LLM_MINIMAX_PROTOCOL', 'LLM_MINIMAX_BASE_URL', 'LLM_MINIMAX_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'minimax' } });
    fireEvent.click(screen.getByRole('button', { name: '+ Add channel' }));
    await screen.findByRole('button', { name: /MiniMax \(official\)/i });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LLM_CHANNELS', value: 'minimax' }),
        expect.objectContaining({ key: 'LLM_MINIMAX_PROTOCOL', value: 'openai' }),
        expect.objectContaining({ key: 'LLM_MINIMAX_BASE_URL', value: 'https://api.minimax.io/v1' }),
        expect.objectContaining({ key: 'LLM_MINIMAX_MODELS', value: 'MiniMax-M3,MiniMax-M2.7,MiniMax-M2.7-highspeed' }),
      ]),
    );
  });

  it('clears active Hermes unsupported multi-key and extra-header env keys on save', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_HERMES_API_KEYS', 'LLM_HERMES_EXTRA_HEADERS'],
      warnings: [
        '检测到已清理 Hermes Phase 3 不支持的配置项：LLM_HERMES_API_KEYS, LLM_HERMES_EXTRA_HEADERS。Hermes reserved channel 只支持单个 LLM_HERMES_API_KEY，不支持多 Key 或额外 Header；如需恢复旧值，请从 .env 备份、Git 历史或桌面端导出备份手动还原，但非空 LLM_HERMES_API_KEYS / LLM_HERMES_EXTRA_HEADERS 仍会被后端校验拒绝。',
      ],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'hermes' },
          { key: 'LLM_HERMES_PROTOCOL', value: 'openai' },
          { key: 'LLM_HERMES_BASE_URL', value: 'http://127.0.0.1:8642/v1' },
          { key: 'LLM_HERMES_ENABLED', value: 'true' },
          { key: 'LLM_HERMES_API_KEY', value: 'sk-hermes-test-value' },
          { key: 'LLM_HERMES_API_KEYS', value: 'sk-old-a,sk-old-b' },
          { key: 'LLM_HERMES_EXTRA_HEADERS', value: '{"X":"Y"}' },
          { key: 'LLM_HERMES_MODELS', value: 'hermes-agent' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Hermes/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), { target: { value: 'hermes-agent,hermes-agent-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    const updateItemMap = new Map(updatePayload.items.map((item: { key: string; value: string }) => [item.key, item.value]));

    expect(updateItemMap.get('LLM_HERMES_API_KEY')).toBe('sk-hermes-test-value');
    expect(updateItemMap.get('LLM_HERMES_API_KEYS')).toBe('');
    expect(updateItemMap.get('LLM_HERMES_EXTRA_HEADERS')).toBe('');
    expect(await screen.findByText(/已清理 Hermes Phase 3 不支持的配置项/i)).toBeInTheDocument();
    expect(screen.getByText(/如需恢复旧值，请从 \.env 备份/i)).toBeInTheDocument();
  });

  it('only persists edited values for runtime-only channel keys', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_CHANNELS', 'LLM_MY_PROXY_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy', rawValueExists: false },
          { key: 'LITELLM_MODEL', value: 'openai/gpt-4o', rawValueExists: false },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai', rawValueExists: false },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1', rawValueExists: false },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true', rawValueExists: false },
          { key: 'LLM_MY_PROXY_API_KEYS', value: 'sk-runtime-only', rawValueExists: false },
          { key: 'LLM_MY_PROXY_MODELS', value: 'gpt-4o-mini', rawValueExists: false },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), { target: { value: 'gpt-4o-mini,gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    const updateItemMap = new Map(updatePayload.items.map((item: { key: string; value: string }) => [item.key, item.value]));

    expect(updateItemMap.get('LLM_MY_PROXY_MODELS')).toBe('gpt-4o-mini,gpt-4o');
    expect(updateItemMap.has('LITELLM_MODEL')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_PROTOCOL')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_BASE_URL')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_API_KEY')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_API_KEYS')).toBe(false);
  });

  it('renames a mixed raw/runtime channel and clears persisted API key field', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_MY_PROXY_API_KEY', 'LLM_MY_PROXY2_API_KEY', 'LLM_MY_PROXY2_BASE_URL', 'LLM_MY_PROXY2_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy' },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai', rawValueExists: false },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1', rawValueExists: false },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true', rawValueExists: false },
          { key: 'LLM_MY_PROXY_API_KEY', value: 'sk-saved' },
          { key: 'LLM_MY_PROXY_MODELS', value: 'gpt-4o-mini', rawValueExists: false },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));
    fireEvent.change(screen.getByLabelText('Channel name'), { target: { value: 'my_proxy2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    const updateItemMap = new Map(updatePayload.items.map((item: { key: string; value: string }) => [item.key, item.value]));

    expect(updateItemMap.get('LLM_MY_PROXY_API_KEY')).toBe('');
    expect(updateItemMap.has('LLM_MY_PROXY_API_KEYS')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_PROTOCOL')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_BASE_URL')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_MODELS')).toBe(false);
    expect(updateItemMap.get('LLM_MY_PROXY2_API_KEY')).toBe('sk-saved');
    expect(updateItemMap.get('LLM_MY_PROXY2_BASE_URL')).toBe('https://proxy.example.com/v1');
    expect(updateItemMap.get('LLM_MY_PROXY2_MODELS')).toBe('gpt-4o-mini');
  });

  it('uses runtime API_KEYS when both API_KEY and API_KEYS coexist', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy', rawValueExists: false },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai', rawValueExists: false },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1', rawValueExists: false },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true', rawValueExists: false },
          { key: 'LLM_MY_PROXY_API_KEY', value: 'sk-saved' },
          { key: 'LLM_MY_PROXY_API_KEYS', value: 'sk-runtime-only', rawValueExists: false },
          { key: 'LLM_MY_PROXY_MODELS', value: 'gpt-4o-mini', rawValueExists: false },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));
    expect(screen.getByLabelText('API Key')).toHaveValue('sk-runtime-only');
  });

  it('does not migrate conflicted API key data as API_KEY when renaming a channel', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_CHANNELS', 'LLM_MY_PROXY_API_KEY', 'LLM_MY_PROXY2_PROTOCOL', 'LLM_MY_PROXY2_BASE_URL', 'LLM_MY_PROXY2_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy', rawValueExists: false },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai', rawValueExists: false },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1', rawValueExists: false },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true', rawValueExists: false },
          { key: 'LLM_MY_PROXY_API_KEY', value: 'sk-saved' },
          { key: 'LLM_MY_PROXY_API_KEYS', value: 'sk-runtime-only', rawValueExists: false },
          { key: 'LLM_MY_PROXY_MODELS', value: 'gpt-4o-mini', rawValueExists: false },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));
    fireEvent.change(screen.getByLabelText('Channel name'), { target: { value: 'my_proxy2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    const updateItemMap = new Map(updatePayload.items.map((item: { key: string; value: string }) => [item.key, item.value]));

    expect(updateItemMap.get('LLM_CHANNELS')).toBe('my_proxy2');
    expect(updateItemMap.has('LLM_MY_PROXY2_API_KEY')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY2_API_KEYS')).toBe(false);
    expect([...updateItemMap.values()]).not.toContain('sk-runtime-only');
    expect([...updateItemMap.values()]).not.toContain('sk-saved');
    expect(updateItemMap.get('LLM_MY_PROXY_API_KEY')).toBe('');
    expect(updateItemMap.get('LLM_MY_PROXY2_BASE_URL')).toBe('https://proxy.example.com/v1');
    expect(updateItemMap.get('LLM_MY_PROXY2_MODELS')).toBe('gpt-4o-mini');
  });

  it('does not migrate runtime-only API keys when renaming a startup-env channel', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_CHANNELS', 'LLM_MY_PROXY2_BASE_URL', 'LLM_MY_PROXY2_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'my_proxy', rawValueExists: false },
          { key: 'LLM_MY_PROXY_PROTOCOL', value: 'openai', rawValueExists: false },
          { key: 'LLM_MY_PROXY_BASE_URL', value: 'https://proxy.example.com/v1', rawValueExists: false },
          { key: 'LLM_MY_PROXY_ENABLED', value: 'true', rawValueExists: false },
          { key: 'LLM_MY_PROXY_API_KEYS', value: 'sk-runtime-only', rawValueExists: false },
          { key: 'LLM_MY_PROXY_MODELS', value: 'gpt-4o-mini', rawValueExists: false },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /my_proxy/i }));
    fireEvent.change(screen.getByLabelText('Channel name'), { target: { value: 'my_proxy2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    const updateItemMap = new Map(updatePayload.items.map((item: { key: string; value: string }) => [item.key, item.value]));

    expect(updateItemMap.get('LLM_CHANNELS')).toBe('my_proxy2');
    expect(updateItemMap.has('LLM_MY_PROXY_API_KEY')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY_API_KEYS')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY2_API_KEY')).toBe(false);
    expect(updateItemMap.has('LLM_MY_PROXY2_API_KEYS')).toBe(false);
    expect([...updateItemMap.values()]).not.toContain('sk-runtime-only');
  });

  it('sanitizes stale runtime models before saving DeepSeek V4 channel changes', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_MODELS', 'LITELLM_MODEL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat,deepseek-reasoner' },
          { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-reasoner' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro,deepseek/deepseek-chat,cohere/command-r-plus' },
          { key: 'VISION_MODEL', value: 'deepseek/deepseek-reasoner' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'deepseek-v4-flash,deepseek-v4-pro' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_MODEL', value: '' }),
        expect.objectContaining({ key: 'AGENT_LITELLM_MODEL', value: '' }),
        expect.objectContaining({ key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro,cohere/command-r-plus' }),
        expect.objectContaining({ key: 'VISION_MODEL', value: '' }),
        expect.objectContaining({ key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-v4-flash,deepseek-v4-pro' }),
      ]),
    );
  });

  it('prompts when bare runtime models loosely match canonical OpenAI route aliases', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_PRIMARY_BASE_URL', 'LITELLM_MODEL', 'LITELLM_FALLBACK_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'primary' },
          { key: 'LLM_PRIMARY_PROTOCOL', value: 'openai' },
          { key: 'LLM_PRIMARY_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_PRIMARY_ENABLED', value: 'true' },
          { key: 'LLM_PRIMARY_API_KEY', value: 'sk-test' },
          { key: 'LLM_PRIMARY_MODELS', value: 'gpt-4o-mini' },
          { key: 'LITELLM_MODEL', value: 'gpt-4o-mini' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'gpt-4o-mini' },
          { key: 'AGENT_LITELLM_MODEL', value: '' },
          { key: 'VISION_MODEL', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByText('primary').closest('[role="button"]') as HTMLElement);
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.example.com/compatible/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(screen.getByText('The current runtime model uses a non-canonical route alias; reselect a canonical model from the dropdown.')).toBeInTheDocument();
    });

    expect(update).not.toHaveBeenCalled();
  });

  it('does not treat direct-env provider models as non-canonical route aliases', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_PRIMARY_BASE_URL', 'LITELLM_MODEL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'primary' },
          { key: 'LLM_PRIMARY_PROTOCOL', value: 'openai' },
          { key: 'LLM_PRIMARY_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_PRIMARY_ENABLED', value: 'true' },
          { key: 'LLM_PRIMARY_API_KEY', value: 'sk-test' },
          { key: 'LLM_PRIMARY_MODELS', value: 'cohere/command-r-plus' },
          { key: 'LITELLM_MODEL', value: 'cohere/command-r-plus' },
          { key: 'AGENT_LITELLM_MODEL', value: '' },
          { key: 'LITELLM_FALLBACK_MODELS', value: '' },
          { key: 'VISION_MODEL', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        modelProviderPrefixes={['cohere', 'openai']}
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByText('primary').closest('[role="button"]') as HTMLElement);
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.example.com/compatible/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_MODEL', value: 'cohere/command-r-plus' }),
      ]),
    );
  });

  it('uses backend provider metadata to preserve direct provider routes in runtime selections', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LITELLM_MODEL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'gateway' },
          { key: 'LLM_GATEWAY_PROTOCOL', value: 'openai' },
          { key: 'LLM_GATEWAY_BASE_URL', value: 'https://gateway.example.com/v1' },
          { key: 'LLM_GATEWAY_ENABLED', value: 'true' },
          { key: 'LLM_GATEWAY_API_KEY', value: 'sk-test' },
          { key: 'LLM_GATEWAY_MODELS', value: 'xai/grok-beta,deepseek-ai/DeepSeek-V3' },
          { key: 'LITELLM_MODEL', value: '' },
          { key: 'AGENT_LITELLM_MODEL', value: '' },
          { key: 'LITELLM_FALLBACK_MODELS', value: '' },
          { key: 'VISION_MODEL', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        modelProviderPrefixes={['openai', 'xai']}
        onSaved={() => {}}
      />,
    );

    expect(selectOptionValues('Primary model')).toEqual(expect.arrayContaining([
      'xai/grok-beta',
      'openai/deepseek-ai/DeepSeek-V3',
    ]));
    expect(selectOptionValues('Primary model')).not.toContain('openai/xai/grok-beta');

    fireEvent.change(screen.getByLabelText('Primary model'), {
      target: { value: 'xai/grok-beta' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Save AI configuration/ }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][0].items).toContainEqual({
      key: 'LITELLM_MODEL',
      value: 'xai/grok-beta',
    });
  });

  it('sanitizes stale runtime models when enabled channels have no available models', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_BASE_URL', 'LITELLM_MODEL', 'AGENT_LITELLM_MODEL', 'LITELLM_FALLBACK_MODELS', 'VISION_MODEL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'false' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat,deepseek-v4-pro' },
          { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro' },
          { key: 'VISION_MODEL', value: 'deepseek/deepseek-chat' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.deepseek.com/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_MODEL', value: '' }),
        expect.objectContaining({ key: 'AGENT_LITELLM_MODEL', value: '' }),
        expect.objectContaining({ key: 'LITELLM_FALLBACK_MODELS', value: '' }),
        expect.objectContaining({ key: 'VISION_MODEL', value: '' }),
      ]),
    );
  });

  it('keeps legacy-key-backed runtime models when enabled channels have no available models', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_PRIMARY_BASE_URL', 'LITELLM_MODEL', 'LITELLM_FALLBACK_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'primary' },
          { key: 'LLM_PRIMARY_PROTOCOL', value: 'openai' },
          { key: 'LLM_PRIMARY_BASE_URL', value: 'https://api.example.com/v1' },
          { key: 'LLM_PRIMARY_ENABLED', value: 'false' },
          { key: 'LLM_PRIMARY_API_KEY', value: 'sk-test' },
          { key: 'LLM_PRIMARY_MODELS', value: 'gpt-4o-mini' },
          { key: 'OPENAI_API_KEY', value: 'sk-legacy-value' },
          { key: 'LITELLM_MODEL', value: 'openai/gpt-4o-mini' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'openai/gpt-4o' },
          { key: 'AGENT_LITELLM_MODEL', value: '' },
          { key: 'VISION_MODEL', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByText('primary').closest('[role="button"]') as HTMLElement);
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.example.com/compatible/v1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_MODEL', value: 'openai/gpt-4o-mini' }),
        expect.objectContaining({ key: 'LITELLM_FALLBACK_MODELS', value: 'openai/gpt-4o' }),
      ]),
    );
  });

  it('shows cleanup warning and restore path after stale runtime models are removed on save', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_MODELS', 'LITELLM_MODEL'],
      warnings: [
        '检测到已同步清理失效的运行时模型引用：主模型 / Agent 主模型 / Vision 模型 / 备选模型中的失效项。如需恢复，请先补回对应渠道模型列表后重新选择；也可用桌面端导出备份或手动 .env 还原之前的 LLM_* / LITELLM_MODEL / AGENT_LITELLM_MODEL / VISION_MODEL / LLM_TEMPERATURE。',
      ],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat,deepseek-reasoner' },
          { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-reasoner' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro,deepseek/deepseek-chat' },
          { key: 'VISION_MODEL', value: 'deepseek/deepseek-reasoner' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'deepseek-v4-flash,deepseek-v4-pro' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    expect(await screen.findByText('Post-save notice')).toBeInTheDocument();
    expect(screen.getByText(/已同步清理失效的运行时模型引用/i)).toBeInTheDocument();
    expect(screen.getByText(/桌面端导出备份或手动 \.env 还原/i)).toBeInTheDocument();
  });

  it('keeps save warnings visible after onSaved-driven refresh', async () => {
    const warningMessage = '检测到已同步清理失效的运行时模型引用：主模型 / Agent 主模型 / Vision 模型 / 备选模型中的失效项。';
    const initialItems = [
      { key: 'LLM_CHANNELS', value: 'deepseek' },
      { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
      { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
      { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
      { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
      { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat,deepseek-reasoner' },
      { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
      { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-reasoner' },
      { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro,cohere/command-r-plus' },
      { key: 'VISION_MODEL', value: 'deepseek/deepseek-reasoner' },
    ];
    const Component = () => {
      const [items, setItems] = useState(initialItems);

      return (
        <LLMChannelEditor
          items={items}
          configVersion="v1"
          maskToken="******"
          onSaved={async (updatedItems) => {
            setItems(updatedItems);
          }}
        />
      );
    };

    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_MODELS', 'LITELLM_MODEL'],
      warnings: [warningMessage],
    });

    render(<Component />);

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'deepseek-v4-flash,deepseek-v4-pro' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    expect(await screen.findByText('Post-save notice')).toBeInTheDocument();
    expect(screen.getByText(warningMessage)).toBeInTheDocument();
  });

  it('clears failed-save feedback after saved props refresh', async () => {
    const initialItems = [
      { key: 'LLM_CHANNELS', value: 'openai' },
      { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
      { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
      { key: 'LLM_OPENAI_ENABLED', value: 'true' },
      { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
      { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' },
    ];
    const onSaved = vi.fn(async () => {
      throw new Error('refresh failed');
    });

    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_OPENAI_BASE_URL'],
      warnings: [],
    });

    const renderResult = render(
      <LLMChannelEditor
        items={initialItems}
        configVersion="v1"
        maskToken="******"
        onSaved={onSaved}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.openai.com/v1/test' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    expect(await screen.findByText('refresh failed')).toBeInTheDocument();

    const savedItems = update.mock.calls[0][0].items;
    renderResult.rerender(
      <LLMChannelEditor
        items={savedItems}
        configVersion="v2"
        maskToken="******"
        onSaved={onSaved}
      />,
    );

    await waitFor(() => {
      expect(screen.queryByText('refresh failed')).not.toBeInTheDocument();
    });
  });

  it('keeps stale runtime fallback model available when user restores it in channel models', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_MODELS', 'LITELLM_MODEL', 'LITELLM_FALLBACK_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat' },
          { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'AGENT_LITELLM_MODEL', value: '' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-old' },
          { key: 'VISION_MODEL', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Models (comma-separated)'), {
      target: { value: 'deepseek-chat,deepseek-old' },
    });

    expect(await screen.findByLabelText('deepseek/deepseek-old')).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));
    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-old' }),
      ]),
    );
  });

  it('keeps runtime selections while channel models are edited temporarily', async () => {
    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-chat,deepseek-reasoner,deepseek-v4-pro' },
          { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
          { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-reasoner' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'deepseek/deepseek-v4-pro' },
          { key: 'VISION_MODEL', value: 'deepseek/deepseek-reasoner' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    const primaryModelSelect = screen.getByRole('combobox', { name: 'Primary model' });
    const agentModelSelect = screen.getByRole('combobox', { name: 'Agent primary model' });
    const visionModelSelect = screen.getByRole('combobox', { name: 'Vision model' });

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    const modelInput = screen.getByLabelText('Models (comma-separated)');
    fireEvent.change(modelInput, {
      target: { value: 'deepseek-v4-flash' },
    });

    await waitFor(() => {
      expect(primaryModelSelect).toHaveValue('deepseek/deepseek-chat');
      expect(agentModelSelect).toHaveValue('deepseek/deepseek-reasoner');
      expect(visionModelSelect).toHaveValue('deepseek/deepseek-reasoner');
    });

    fireEvent.change(modelInput, {
      target: { value: 'deepseek-chat,deepseek-reasoner,deepseek-v4-pro' },
    });

    await waitFor(() => {
      expect(primaryModelSelect).toHaveValue('deepseek/deepseek-chat');
      expect(agentModelSelect).toHaveValue('deepseek/deepseek-reasoner');
      expect(visionModelSelect).toHaveValue('deepseek/deepseek-reasoner');
      expect(screen.getByLabelText('deepseek/deepseek-v4-pro')).toBeChecked();
    });
  });

  it('keeps direct-env provider runtime models (cohere / google / xai) while saving channel changes', async () => {
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DEEPSEEK_BASE_URL', 'LITELLM_MODEL', 'AGENT_LITELLM_MODEL', 'LITELLM_FALLBACK_MODELS', 'VISION_MODEL'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
          { key: 'LLM_DEEPSEEK_BASE_URL', value: 'https://api.deepseek.com/v1' },
          { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
          { key: 'LLM_DEEPSEEK_API_KEY', value: 'sk-test' },
          { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-v4-flash' },
          { key: 'LITELLM_MODEL', value: 'cohere/command-r-plus' },
          { key: 'AGENT_LITELLM_MODEL', value: 'google/gemini-2.5-flash' },
          { key: 'LITELLM_FALLBACK_MODELS', value: 'cohere/command-r-plus,google/gemini-2.5-flash,xai/grok-beta' },
          { key: 'VISION_MODEL', value: 'xai/grok-vision-beta' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /DeepSeek \(official\)/i }));
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.deepseek.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LITELLM_MODEL', value: 'cohere/command-r-plus' }),
        expect.objectContaining({ key: 'AGENT_LITELLM_MODEL', value: 'google/gemini-2.5-flash' }),
        expect.objectContaining({ key: 'LITELLM_FALLBACK_MODELS', value: 'cohere/command-r-plus,google/gemini-2.5-flash,xai/grok-beta' }),
        expect.objectContaining({ key: 'VISION_MODEL', value: 'xai/grok-vision-beta' }),
      ]),
    );
  });

  it('checks protocol-prefixed selected model when discovery returns bare id', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: true,
      message: 'LLM channel model discovery succeeded',
      error: null,
      resolvedProtocol: 'openai',
      models: ['MiniMax-M1'],
      latencyMs: 80,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'dashscope' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'sk-test' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'openai/MiniMax-M1' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Qwen/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    const checkbox = await screen.findByLabelText('MiniMax-M1');
    expect(checkbox).toBeChecked();

    fireEvent.click(checkbox);
    await waitFor(() => {
      expect(screen.getByLabelText('Manual models (comma-separated)')).toHaveValue('');
    });
  });

  it('does not treat unknown-prefixed selected model as equivalent to bare discovered id', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: true,
      message: 'LLM channel model discovery succeeded',
      error: null,
      resolvedProtocol: 'openai',
      models: ['MiniMax-M1'],
      latencyMs: 80,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'dashscope' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'sk-test' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'minimax/MiniMax-M1' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Qwen/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    const checkbox = await screen.findByLabelText('MiniMax-M1');
    expect(checkbox).not.toBeChecked();
    expect(screen.getByLabelText('Manual models (comma-separated)')).toHaveValue('minimax/MiniMax-M1');
  });

  it('discovers models and writes selected values back to channel config', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: true,
      message: 'LLM channel model discovery succeeded',
      error: null,
      resolvedProtocol: 'openai',
      models: ['qwen-plus', 'qwen-turbo'],
      latencyMs: 88,
    });
    update.mockResolvedValue({
      success: true,
      configVersion: 'v2',
      appliedCount: 1,
      skippedMaskedCount: 0,
      reloadTriggered: true,
      updatedKeys: ['LLM_DASHSCOPE_MODELS'],
      warnings: [],
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'dashscope' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'sk-test' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'qwen-old' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Dashscope/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    const qwenPlusCheckbox = await screen.findByLabelText('qwen-plus');
    fireEvent.click(qwenPlusCheckbox);

    await waitFor(() => {
      expect(screen.getByLabelText('Manual models (comma-separated)')).toHaveValue('qwen-old,qwen-plus');
    });

    expect(discoverLLMChannelModels).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'dashscope',
        protocol: 'openai',
        baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        apiKey: 'sk-test',
        models: ['qwen-old'],
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save AI configuration' }));

    await waitFor(() => {
      expect(update).toHaveBeenCalled();
    });

    const updatePayload = update.mock.calls[0][0];
    expect(updatePayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'LLM_DASHSCOPE_MODELS', value: 'qwen-old,qwen-plus' }),
      ]),
    );
  });

  it('shows structured troubleshooting hint when channel auth fails', async () => {
    testLLMChannel.mockResolvedValue({ success: false, message: 'LLM authentication failed', error: '401 Unauthorized · Bearer [REDACTED]', errorCode: 'auth', stage: 'chat_completion', retryable: false, details: {}, resolvedProtocol: 'openai', resolvedModel: 'openai/gpt-4o-mini', latencyMs: null });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText(/Chat completion · Authentication failed: LLM authentication failed/i)).toBeInTheDocument();
    expect(screen.getByText(/Check that the API Key is correct/i)).toBeInTheDocument();
    expect(screen.queryByText(/reorder the model list or remove the unavailable model/i)).not.toBeInTheDocument();
  });

  it('shows tested model and model-availability hints when a model is disabled', async () => {
    testLLMChannel.mockResolvedValue({
      success: false,
      message: 'LLM channel test failed',
      error: 'litellm.APIError: APIError: OpenAIException - Model disabled.',
      errorCode: 'model_not_found',
      stage: 'chat_completion',
      retryable: false,
      details: { reason: 'model_access_denied', model: 'openai/deepseek-ai/DeepSeek-V3' },
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/deepseek-ai/DeepSeek-V3',
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'siliconflow' },
          { key: 'LLM_SILICONFLOW_PROTOCOL', value: 'openai' },
          { key: 'LLM_SILICONFLOW_BASE_URL', value: 'https://api.siliconflow.cn/v1' },
          { key: 'LLM_SILICONFLOW_ENABLED', value: 'true' },
          { key: 'LLM_SILICONFLOW_API_KEY', value: 'secret-key' },
          { key: 'LLM_SILICONFLOW_MODELS', value: 'deepseek-ai/DeepSeek-V3,Qwen/Qwen3-Coder' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /SiliconFlow/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText(/Chat completion · Model unavailable: LLM channel test failed/i)).toBeInTheDocument();
    expect(screen.getByText(/Model tested this time: openai\/deepseek-ai\/DeepSeek-V3/i)).toBeInTheDocument();
    expect(screen.getByText(/The basic connection test uses the first model in the list by default: deepseek-ai\/DeepSeek-V3/i)).toBeInTheDocument();
    expect(screen.getByText(/The basic connection test only tests the first model in the model list by default/i)).toBeInTheDocument();
    expect(screen.getByText(/reorder the model list or remove the unavailable model/i)).toBeInTheDocument();
    expect(screen.getByText(/confirm the model is enabled, visible to this account/i)).toBeInTheDocument();
    expect(screen.queryByText(/Base URL, proxy, TLS/i)).not.toBeInTheDocument();
    expect(testLLMChannel).toHaveBeenCalledWith(expect.objectContaining({
      models: ['deepseek-ai/DeepSeek-V3', 'Qwen/Qwen3-Coder'],
    }));
  });

  it('shows provider blocked troubleshooting without network or model-list hints', async () => {
    testLLMChannel.mockResolvedValue({
      success: false,
      message: 'LLM request was blocked by provider or gateway policy',
      error: 'litellm.APIError: APIError: OpenAIException - Your request was blocked.',
      errorCode: 'request_blocked',
      stage: 'chat_completion',
      retryable: false,
      details: { reason: 'provider_blocked', model: 'openai/gpt-5.5' },
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-5.5',
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'proxy' },
          { key: 'LLM_PROXY_PROTOCOL', value: 'openai' },
          { key: 'LLM_PROXY_BASE_URL', value: 'https://gateway.example.com/v1' },
          { key: 'LLM_PROXY_ENABLED', value: 'true' },
          { key: 'LLM_PROXY_API_KEY', value: 'secret-key' },
          { key: 'LLM_PROXY_MODELS', value: 'gpt-5.5,gpt-4o-mini' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /proxy/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText(/Chat completion · Request blocked/i)).toBeInTheDocument();
    expect(screen.getByText(/Model tested this time: openai\/gpt-5\.5/i)).toBeInTheDocument();
    expect(screen.getByText(/account risk controls, region restrictions, model permissions/i)).toBeInTheDocument();
    expect(screen.queryByText(/Base URL, proxy, TLS/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/reorder the model list or remove the unavailable model/i)).not.toBeInTheDocument();
  });

  it('shows focused quota exceeded troubleshooting hints', async () => {
    testLLMChannel.mockResolvedValue({
      success: false,
      message: 'LLM request was rejected by quota or rate limiting',
      error: 'quota exceeded',
      errorCode: 'quota',
      stage: 'chat_completion',
      retryable: true,
      details: { reason: 'quota_exceeded' },
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-4o-mini',
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText(/The provider reports quota exhausted/i)).toBeInTheDocument();
    expect(screen.queryByText(/reorder the model list or remove the unavailable model/i)).not.toBeInTheDocument();
  });

  it('does not show model-list action hints for network failures', async () => {
    testLLMChannel.mockResolvedValue({
      success: false,
      message: 'LLM request failed before a valid response was returned',
      error: 'DNS lookup failed',
      errorCode: 'network_error',
      stage: 'chat_completion',
      retryable: true,
      details: { reason: 'dns_error' },
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-4o-mini',
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    expect(await screen.findByText(/DNS resolution failed/i)).toBeInTheDocument();
    expect(screen.queryByText(/reorder the model list or remove the unavailable model/i)).not.toBeInTheDocument();
  });

  it('does not request runtime capabilities during the basic connection test', async () => {
    testLLMChannel.mockResolvedValue({
      success: true,
      message: 'LLM channel test succeeded',
      error: null,
      errorCode: null,
      stage: 'chat_completion',
      retryable: false,
      details: {},
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-4o-mini',
      latencyMs: 80,
      capabilityResults: {},
    });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    await screen.findByText(/Connection successful · openai\/gpt-4o-mini/i);
    expect(testLLMChannel).toHaveBeenCalledWith(expect.not.objectContaining({ capabilityChecks: expect.anything() }));
  });

  it('runs explicit runtime capability checks and shows detailed hints', async () => {
    testLLMChannel.mockResolvedValue({
      success: true,
      message: 'LLM channel test succeeded',
      error: null,
      errorCode: null,
      stage: 'chat_completion',
      retryable: false,
      details: {},
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-4o-mini',
      latencyMs: 80,
      capabilityResults: {
        json: {
          status: 'passed',
          message: 'JSON output capability check passed',
          errorCode: null,
          stage: 'capability_json',
          retryable: false,
          details: { reason: 'json_valid' },
        },
        tools: {
          status: 'failed',
          message: 'LLM channel does not support tools capability',
          errorCode: 'capability_unsupported',
          stage: 'capability_tools',
          retryable: false,
          details: { reason: 'capability_unsupported' },
        },
      },
    });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByLabelText('JSON'));
    fireEvent.click(screen.getByLabelText('Tools'));
    fireEvent.click(screen.getByRole('button', { name: 'Check capabilities' }));

    expect(await screen.findByText(/Capability check complete: 1 passed \/ 1 failed \/ 0 skipped/i)).toBeInTheDocument();
    expect(screen.getByText('JSON Passed')).toBeInTheDocument();
    expect(screen.getByText('Tools Failed')).toBeInTheDocument();
    expect(screen.getByText(/does not support this capability/i)).toBeInTheDocument();
    expect(testLLMChannel).toHaveBeenCalledWith(expect.objectContaining({ capabilityChecks: ['json', 'tools'] }));
  });

  it('shows skipped runtime capabilities when the base test fails', async () => {
    testLLMChannel.mockResolvedValue({
      success: false,
      message: 'LLM authentication failed',
      error: '401 Unauthorized',
      errorCode: 'auth',
      stage: 'chat_completion',
      retryable: false,
      details: { reason: 'api_key_rejected' },
      resolvedProtocol: 'openai',
      resolvedModel: 'openai/gpt-4o-mini',
      latencyMs: null,
      capabilityResults: {
        json: {
          status: 'skipped',
          message: 'Skipped because the base channel test did not pass',
          errorCode: 'skipped',
          stage: 'capability_json',
          retryable: false,
          details: { reason: 'base_test_failed' },
        },
      },
    });

    render(
      <LLMChannelEditor
        items={[{ key: 'LLM_CHANNELS', value: 'openai' }, { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' }, { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' }, { key: 'LLM_OPENAI_ENABLED', value: 'true' }, { key: 'LLM_OPENAI_API_KEY', value: 'bad-key' }, { key: 'LLM_OPENAI_MODELS', value: 'gpt-4o-mini' }]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByLabelText('JSON'));
    fireEvent.click(screen.getByRole('button', { name: 'Check capabilities' }));

    expect(await screen.findByText(/Capability check complete: 0 passed \/ 0 failed \/ 1 skipped/i)).toBeInTheDocument();
    expect(screen.getByText('JSON Skipped')).toBeInTheDocument();
    expect(screen.getByText(/The provider rejected the current API Key/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Models (comma-separated)')).toBeEnabled();
  });

  it('keeps manual model input available when discovery fails', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: false,
      message: 'Model discovery is not supported for this protocol',
      error: 'LLM channel does not support /models discovery yet',
      errorCode: 'unsupported_protocol',
      stage: 'model_discovery',
      retryable: false,
      details: {},
      resolvedProtocol: 'gemini',
      models: [],
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'gemini' },
          { key: 'LLM_GEMINI_PROTOCOL', value: 'gemini' },
          { key: 'LLM_GEMINI_BASE_URL', value: '' },
          { key: 'LLM_GEMINI_ENABLED', value: 'true' },
          { key: 'LLM_GEMINI_API_KEY', value: 'sk-test' },
          { key: 'LLM_GEMINI_MODELS', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Gemini \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    await screen.findByText(/Model discovery · Protocol not yet supported: Model discovery is not supported for this protocol/i);
    expect(screen.getByText(/Automatic model discovery is only available for OpenAI Compatible \/ DeepSeek channels/i)).toBeInTheDocument();

    const manualInput = screen.getByLabelText('Models (comma-separated)');
    fireEvent.change(manualInput, { target: { value: 'gemini-2.5-flash' } });
    expect(manualInput).toHaveValue('gemini-2.5-flash');
  });

  it('maps discovery format errors to the /models troubleshooting hint', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: false,
      message: 'Failed to parse /models response',
      error: 'Unexpected discovery payload',
      errorCode: 'format_error',
      stage: 'response_parse',
      retryable: false,
      details: {},
      resolvedProtocol: 'openai',
      models: [],
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
          { key: 'LLM_OPENAI_MODELS', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    expect(await screen.findByText(/Response parsing · Format error: Failed to parse \/models response/i)).toBeInTheDocument();
    expect(screen.getByText(/The \/models response format returned by this channel is incompatible/i)).toBeInTheDocument();
  });

  it('maps discovery empty responses to the /models troubleshooting hint', async () => {
    discoverLLMChannelModels.mockResolvedValue({
      success: false,
      message: 'No model IDs returned from /models response',
      error: 'Empty model discovery response',
      errorCode: 'empty_response',
      stage: 'model_discovery',
      retryable: false,
      details: {},
      resolvedProtocol: 'openai',
      models: [],
      latencyMs: null,
    });

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'secret-key' },
          { key: 'LLM_OPENAI_MODELS', value: '' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    expect(await screen.findByText(/Model discovery · Empty response: No model IDs returned from \/models response/i)).toBeInTheDocument();
    expect(screen.getByText(/did not return any usable model IDs/i)).toBeInTheDocument();
    expect(screen.queryByText(/switching to a compatible model or disabling extra response modes/i)).not.toBeInTheDocument();
  });

  it('does not apply stale discovery response after channel list re-sync', async () => {
    let resolvePendingFirst!: (value: unknown) => void;
    const pendingFirst = new Promise((resolve) => {
      resolvePendingFirst = resolve;
    });

    discoverLLMChannelModels
      .mockImplementationOnce(() => pendingFirst)
      .mockResolvedValueOnce({
        success: true,
        message: 'LLM channel model discovery succeeded',
        error: null,
        resolvedProtocol: 'openai',
        models: ['dashscope-plus'],
        latencyMs: 30,
      });

    const renderResult = render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'openai' },
          { key: 'LLM_OPENAI_PROTOCOL', value: 'openai' },
          { key: 'LLM_OPENAI_BASE_URL', value: 'https://api.openai.com/v1' },
          { key: 'LLM_OPENAI_ENABLED', value: 'true' },
          { key: 'LLM_OPENAI_API_KEY', value: 'open-key' },
          { key: 'LLM_OPENAI_MODELS', value: 'gpt-old' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'dash-key' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'dash-old' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /OpenAI \(official\)/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    renderResult.rerender(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'dashscope' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'dash-key' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'dash-old' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Qwen/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    const dashModelCheckbox = await screen.findByLabelText('dashscope-plus');
    fireEvent.click(dashModelCheckbox);

    expect(screen.getByLabelText('Manual models (comma-separated)')).toHaveValue('dash-old,dashscope-plus');

    resolvePendingFirst({
      success: true,
      message: 'LLM channel model discovery succeeded',
      error: null,
      resolvedProtocol: 'openai',
      models: ['stale-openai'],
      latencyMs: 20,
    });

    await waitFor(() => {
      expect(screen.getByLabelText('Manual models (comma-separated)')).toHaveValue('dash-old,dashscope-plus');
    });
    expect(screen.queryByLabelText('stale-openai')).not.toBeInTheDocument();
  });

  it('does not apply stale discovery response after inline channel edit', async () => {
    let resolvePendingFirst!: (value: unknown) => void;
    const pendingFirst = new Promise((resolve) => {
      resolvePendingFirst = resolve;
    });

    discoverLLMChannelModels.mockImplementationOnce(() => pendingFirst);

    render(
      <LLMChannelEditor
        items={[
          { key: 'LLM_CHANNELS', value: 'dashscope' },
          { key: 'LLM_DASHSCOPE_PROTOCOL', value: 'openai' },
          { key: 'LLM_DASHSCOPE_BASE_URL', value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
          { key: 'LLM_DASHSCOPE_ENABLED', value: 'true' },
          { key: 'LLM_DASHSCOPE_API_KEY', value: 'dash-key' },
          { key: 'LLM_DASHSCOPE_MODELS', value: 'qwen-old' },
        ]}
        configVersion="v1"
        maskToken="******"
        onSaved={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Dashscope/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Fetch models' }));

    const baseUrlInput = screen.getByLabelText('Base URL');
    fireEvent.change(baseUrlInput, {
      target: { value: 'https://dashscope.aliyuncs.com/compatible-mode/v2' },
    });

    resolvePendingFirst({
      success: true,
      message: 'LLM channel model discovery succeeded',
      error: null,
      resolvedProtocol: 'openai',
      models: ['stale-openai'],
      latencyMs: 20,
    });

    await waitFor(() => {
      expect(screen.getByLabelText('Models (comma-separated)')).toHaveValue('qwen-old');
      expect(screen.queryByLabelText('stale-openai')).not.toBeInTheDocument();
    });
  });
});
