# -*- coding: utf-8 -*-
"""
===================================
帮助命令
===================================

显示可用命令列表和使用说明。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class HelpCommand(BotCommand):
    """
    帮助命令
    
    显示所有可用命令的列表和使用说明。
    也可以查看特定命令的详细帮助。
    
    用法：
        /help         - 显示所有命令
        /help analyze - 显示 analyze 命令的详细帮助
    """
    
    @property
    def name(self) -> str:
        return "help"
    
    @property
    def aliases(self) -> List[str]:
        return ["h", "帮助", "?"]
    
    @property
    def description(self) -> str:
        return "Show help information"
    
    @property
    def usage(self) -> str:
        return "/help [command]"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行帮助命令"""
        # 延迟导入避免循环依赖
        from bot.dispatcher import get_dispatcher
        
        dispatcher = get_dispatcher()
        
        # 如果指定了命令名，显示该命令的详细帮助
        if args:
            cmd_name = args[0]
            command = dispatcher.get_command(cmd_name)
            
            if command is None:
                return BotResponse.error_response(f"Unknown command: {cmd_name}")
            
            # 构建详细帮助
            help_text = self._format_command_help(command, dispatcher.command_prefix)
            return BotResponse.markdown_response(help_text)
        
        # 显示所有命令列表
        commands = dispatcher.list_commands(include_hidden=False)
        prefix = dispatcher.command_prefix
        
        help_text = self._format_help_list(commands, prefix)
        return BotResponse.markdown_response(help_text)
    
    def _format_help_list(self, commands: List[BotCommand], prefix: str) -> str:
        """格式化命令列表"""
        lines = [
            "📚 **Stock Analysis Assistant - Command Help**",
            "",
            "Available commands:",
            "",
        ]
        
        for cmd in commands:
            # 命令名和别名
            aliases_str = ""
            if cmd.aliases:
                # 过滤掉中文别名，只显示英文别名
                en_aliases = [a for a in cmd.aliases if a.isascii()]
                if en_aliases:
                    aliases_str = f" ({', '.join(prefix + a for a in en_aliases[:2])})"
            
            lines.append(f"• {prefix}{cmd.name}{aliases_str} - {cmd.description}")
            lines.append("")

        lines.extend([
            "",
            "---",
            f"💡 Type {prefix}help <command> for detailed usage",
            "",
            "**Examples:**",
            "",
            f"• {prefix}analyze AAPL - analyze a single stock",
            "",
            f"• {prefix}market - run the market review",
            "",
            f"• {prefix}batch - analyze your watchlist",
        ])
        
        return "\n".join(lines)
    
    def _format_command_help(self, command: BotCommand, prefix: str) -> str:
        """格式化单个命令的详细帮助"""
        lines = [
            f"📖 **{prefix}{command.name}** - {command.description}",
            "",
            f"**Usage:** `{command.usage}`",
            "",
        ]
        
        # 别名
        if command.aliases:
            aliases = [f"`{prefix}{a}`" for a in command.aliases if a.isascii()]
            if aliases:
                lines.append(f"**Aliases:** {', '.join(aliases)}")
                lines.append("")
        
        # 权限
        if command.admin_only:
            lines.append("⚠️ **Requires admin privileges**")
            lines.append("")
        
        return "\n".join(lines)
