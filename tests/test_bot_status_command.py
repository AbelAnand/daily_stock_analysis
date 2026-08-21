# -*- coding: utf-8 -*-
"""Tests for bot /status command output."""

from bot.commands.status import StatusCommand
from src.config import Config


def test_status_command_reports_unified_llm_and_notification_channels():
    model_list = [
        {
            "model_name": "deepseek/deepseek-v4-flash",
            "litellm_params": {
                "model": "deepseek/deepseek-v4-flash",
                "api_key": "sk-test",
            },
        }
    ]
    config = Config(
        stock_list=["600519", "AAPL"],
        litellm_model="deepseek/deepseek-v4-flash",
        agent_litellm_model="openai/gpt-4o-mini",
        llm_channels=[
            {
                "name": "deepseek",
                "models": ["deepseek/deepseek-v4-flash"],
            }
        ],
        llm_models_source="llm_channels",
        llm_model_list=model_list,
        custom_webhook_urls=["https://example.com/webhook"],
        slack_webhook_url="https://hooks.slack.com/services/T/B/C",
        serverchan3_sendkey="SCT123",
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is True
    assert "Primary model: deepseek/deepseek-v4-flash" in text
    assert "Agent model: openai/gpt-4o-mini" in text
    assert "LLM channels: deepseek" in text
    assert "Custom Webhook: ✅" in text
    assert "Slack: ✅" in text
    assert "PushPlus/Pushover/ServerChan3: ✅" in text
    assert "System ready" in text


def test_status_command_warns_when_no_llm_source_configured():
    config = Config(stock_list=["600519"])
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is False
    assert "Primary model: not configured" in text
    assert "AI service not configured" in text
    assert "LITELLM_MODEL" in text


def test_status_command_does_not_treat_managed_model_name_as_ready():
    config = Config(
        stock_list=["600519"],
        litellm_model="openai/gpt-4o-mini",
        llm_model_list=[],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is False
    assert "AI service not configured" in text


def test_status_command_keeps_channel_mode_priority_over_legacy_keys():
    config = Config(
        stock_list=["600519"],
        litellm_model="openai/gpt-4o-mini",
        llm_channels=[
            {
                "name": "deepseek",
                "models": ["deepseek/deepseek-v4-flash"],
            }
        ],
        llm_models_source="llm_channels",
        llm_model_list=[
            {
                "model_name": "deepseek/deepseek-v4-flash",
                "litellm_params": {
                    "model": "deepseek/deepseek-v4-flash",
                    "api_key": "sk-test",
                },
            }
        ],
        openai_api_keys=["openai-legacy-key"],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is False
    assert "AI service not configured" in text
    assert "Primary model: openai/gpt-4o-mini" in text


def test_status_command_requires_primary_model_in_configured_router_models():
    config = Config(
        stock_list=["600519"],
        litellm_model="openai/gpt-4o-mini",
        llm_channels=[
            {
                "name": "deepseek",
                "models": ["deepseek/deepseek-v4-flash"],
            }
        ],
        llm_models_source="llm_channels",
        llm_model_list=[
            {
                "model_name": "deepseek/deepseek-v4-flash",
                "litellm_params": {
                    "model": "deepseek/deepseek-v4-flash",
                    "api_key": "sk-test",
                },
            }
        ],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is False
    assert "AI service not configured" in text
    assert "System ready" not in text


def test_status_command_requires_primary_model_for_yaml_router_models():
    config = Config(
        stock_list=["600519"],
        litellm_model="",
        llm_models_source="litellm_config",
        llm_model_list=[
            {
                "model_name": "yaml-primary",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-test",
                },
            }
        ],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_yaml"] is True
    assert status["ai_available"] is False
    assert "Primary model: not configured" in text
    assert "AI service not configured" in text
    assert "System ready" not in text


def test_status_command_does_not_treat_invalid_yaml_path_as_active():
    config = Config(
        stock_list=["600519"],
        litellm_config_path="missing.yaml",
        llm_models_source="legacy_env",
        llm_model_list=[],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_yaml"] is False
    assert status["ai_available"] is False
    assert "LiteLLM YAML: ❌" in text
    assert "AI service not configured" in text


def test_status_command_treats_direct_env_provider_model_as_ready():
    config = Config(
        stock_list=["600519"],
        litellm_model="cohere/command-r-plus",
        llm_model_list=[],
    )
    command = StatusCommand()

    status = command._collect_status(config)
    text = command._format_status(status, "telegram")

    assert status["ai_available"] is True
    assert "System ready" in text


def test_status_command_supports_legacy_key_compatibility_without_explicit_litellm_model(monkeypatch, tmp_path):
    # When only legacy OpenAI-compatible keys are configured and LITELLM_MODEL is unset,
    # runtime still infers a usable model path. /status should reflect this compatibility
    # path instead of reporting hard failure.
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    for key in (
        "GEMINI_API_KEYS",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEYS",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEYS",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEYS",
        "AIHUBMIX_KEY",
        "LITELLM_MODEL",
        "LLM_CHANNELS",
        "LITELLM_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-legacy-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")

    Config.reset_instance()
    try:
        config = Config.get_instance()
        command = StatusCommand()

        status = command._collect_status(config)
        text = command._format_status(status, "telegram")

        assert status["ai_available"] is True
        assert "Primary model: openai/gpt-4o-mini" in text
        assert "AI service not configured" not in text
    finally:
        Config.reset_instance()
