# -*- coding: utf-8 -*-
"""
===================================
状态命令
===================================

显示系统运行状态和配置信息。
"""

import platform
import sys
from datetime import datetime
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class StatusCommand(BotCommand):
    """
    状态命令
    
    显示系统运行状态，包括：
    - 服务状态
    - 配置信息
    - 可用功能
    """
    
    @property
    def name(self) -> str:
        return "status"
    
    @property
    def aliases(self) -> List[str]:
        return ["s", "状态", "info"]
    
    @property
    def description(self) -> str:
        return "Show system status"
    
    @property
    def usage(self) -> str:
        return "/status"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行状态命令"""
        from src.config import get_config
        
        config = get_config()
        
        # 收集状态信息
        status_info = self._collect_status(config)
        
        # 格式化输出
        text = self._format_status(status_info, message.platform)
        
        return BotResponse.markdown_response(text)
    
    def _collect_status(self, config) -> dict:
        """收集系统状态信息"""
        from src.config import _uses_direct_env_provider, get_configured_llm_models

        status = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "stock_count": len(config.stock_list),
            "stock_list": config.stock_list[:5],  # 只显示前5个
        }
        
        # AI 配置状态
        llm_channels = getattr(config, "llm_channels", []) or []
        llm_model_list = getattr(config, "llm_model_list", []) or []
        llm_model = (getattr(config, "litellm_model", "") or "").strip()
        agent_model = (getattr(config, "agent_litellm_model", "") or "").strip()
        status["ai_primary_model"] = llm_model
        status["ai_agent_model"] = agent_model or ("inherits primary model" if llm_model else "")
        status["ai_channels"] = [
            str(channel.get("name") or "").strip()
            for channel in llm_channels
            if str(channel.get("name") or "").strip()
        ]
        status["ai_yaml"] = (
            getattr(config, "llm_models_source", "") == "litellm_config"
            and bool(llm_model_list)
        )
        status["ai_legacy_keys"] = {
            "Gemini": bool(getattr(config, "gemini_api_keys", [])),
            "OpenAI": bool(getattr(config, "openai_api_keys", [])),
            "Anthropic": bool(getattr(config, "anthropic_api_keys", [])),
            "DeepSeek": bool(getattr(config, "deepseek_api_keys", [])),
        }
        has_direct_env_model = bool(llm_model) and _uses_direct_env_provider(llm_model)
        available_router_model_set = set(get_configured_llm_models(llm_model_list))
        primary_model_reachable = not (
            available_router_model_set
            and llm_model
            and not _uses_direct_env_provider(llm_model)
            and llm_model not in available_router_model_set
        )
        status["ai_available"] = bool(
            llm_model
            and (has_direct_env_model or (llm_model_list and primary_model_reachable))
        )
        
        # 搜索服务状态
        status["search_bocha"] = len(config.bocha_api_keys) > 0
        status["search_tavily"] = len(config.tavily_api_keys) > 0
        status["search_brave"] = len(config.brave_api_keys) > 0
        status["search_serpapi"] = len(config.serpapi_keys) > 0
        status["search_minimax"] = len(config.minimax_api_keys) > 0
        status["search_searxng"] = config.has_searxng_enabled()
        
        # 通知渠道状态
        status["notify_wechat"] = bool(config.wechat_webhook_url)
        status["notify_feishu"] = bool(config.feishu_webhook_url)
        status["notify_telegram"] = bool(config.telegram_bot_token and config.telegram_chat_id)
        status["notify_email"] = bool(config.email_sender and config.email_password)
        status["notify_custom"] = bool(getattr(config, "custom_webhook_urls", []))
        status["notify_discord"] = bool(
            getattr(config, "discord_webhook_url", None)
            or (
                getattr(config, "discord_bot_token", None)
                and getattr(config, "discord_main_channel_id", None)
            )
        )
        status["notify_slack"] = bool(
            getattr(config, "slack_webhook_url", None)
            or (
                getattr(config, "slack_bot_token", None)
                and getattr(config, "slack_channel_id", None)
            )
        )
        status["notify_push"] = bool(
            getattr(config, "pushplus_token", None)
            or (
                getattr(config, "pushover_user_key", None)
                and getattr(config, "pushover_api_token", None)
            )
            or getattr(config, "serverchan3_sendkey", None)
        )
        
        return status
    
    def _format_status(self, status: dict, platform: str) -> str:
        """格式化状态信息"""
        # 状态图标
        def icon(enabled: bool) -> str:
            return "✅" if enabled else "❌"
        
        lines = [
            "📊 **Stock Analysis Assistant - System Status**",
            "",
            f"🕐 Time: {status['timestamp']}",
            f"🐍 Python: {status['python_version']}",
            f"💻 Platform: {status['platform']}",
            "",
            "---",
            "",
            "**📈 Watchlist**",
            f"• Stock count: {status['stock_count']}",
        ]
        
        if status['stock_list']:
            stocks_preview = ", ".join(status['stock_list'])
            if status['stock_count'] > 5:
                stocks_preview += f" ... ({status['stock_count']} total)"
            lines.append(f"• Stock list: {stocks_preview}")
        
        lines.extend([
            "",
            "**🤖 AI Analysis Service**",
            f"• Primary model: {status['ai_primary_model'] or 'not configured'}",
            f"• Agent model: {status['ai_agent_model'] or 'not configured'}",
            f"• LLM channels: {', '.join(status['ai_channels']) if status['ai_channels'] else 'not configured'}",
            f"• LiteLLM YAML: {icon(status['ai_yaml'])}",
            "• Legacy Key: "
            + ", ".join(
                f"{name}{icon(enabled)}"
                for name, enabled in status["ai_legacy_keys"].items()
            ),
            "",
            "**🔍 Search Services**",
            f"• Bocha: {icon(status['search_bocha'])}",
            f"• Tavily: {icon(status['search_tavily'])}",
            f"• Brave: {icon(status['search_brave'])}",
            f"• SerpAPI: {icon(status['search_serpapi'])}",
            f"• MiniMax: {icon(status['search_minimax'])}",
            f"• SearXNG: {icon(status['search_searxng'])}",
            "",
            "**📢 Notification Channels**",
            f"• WeCom: {icon(status['notify_wechat'])}",
            f"• Feishu: {icon(status['notify_feishu'])}",
            f"• Telegram: {icon(status['notify_telegram'])}",
            f"• Email: {icon(status['notify_email'])}",
            f"• Custom Webhook: {icon(status['notify_custom'])}",
            f"• Discord: {icon(status['notify_discord'])}",
            f"• Slack: {icon(status['notify_slack'])}",
            f"• PushPlus/Pushover/ServerChan3: {icon(status['notify_push'])}",
        ])
        
        # AI 服务总体状态
        if status["ai_available"]:
            lines.extend([
                "",
                "---",
                "✅ **System ready. You can start analyzing!**",
            ])
        else:
            lines.extend([
                "",
                "---",
                "⚠️ **AI service not configured; analysis is unavailable**",
                "Please configure LITELLM_MODEL, LLM_CHANNELS, LITELLM_CONFIG, or any provider API key",
            ])
        
        return "\n".join(lines)
