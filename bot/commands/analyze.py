# -*- coding: utf-8 -*-
"""
===================================
股票分析命令
===================================

分析指定股票，调用 AI 生成分析报告。
"""

import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.services.stock_code_utils import resolve_index_stock_code_for_analysis

logger = logging.getLogger(__name__)


class AnalyzeCommand(BotCommand):
    """
    股票分析命令
    
    分析指定股票代码，生成 AI 分析报告并推送。
    
    用法：
        /analyze 600519       - 分析贵州茅台（精简报告）
        /analyze 600519 full  - 分析并生成完整报告
    """
    
    @property
    def name(self) -> str:
        return "analyze"
    
    @property
    def aliases(self) -> List[str]:
        return ["a", "分析", "查"]
    
    @property
    def description(self) -> str:
        return "Analyze a stock"
    
    @property
    def usage(self) -> str:
        return "/analyze <stock_code> [full]"
    
    def validate_args(self, args: List[str]) -> Optional[str]:
        """验证参数"""
        if not args:
            return "Please provide a stock code"
        
        code = args[0].upper()

        # 验证股票代码格式
        # A股：6位数字
        # 港股：HK+5位数字
        # 美股：1-5个大写字母+.+2个后缀字母
        is_a_stock = re.match(r'^\d{6}$', code)
        is_hk_stock = re.match(r'^HK\d{5}$', code)
        is_us_stock = re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', code)

        if not (is_a_stock or is_hk_stock or is_us_stock):
            return f"Invalid stock code: {code} (A-share: 6 digits / HK: HK + 5 digits / US: 1-5 letters)"
        
        return None
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行分析命令"""
        code = resolve_index_stock_code_for_analysis(args[0])
        
        # 检查是否需要完整报告（默认精简，传 full/完整/详细 切换）
        report_type = "simple"
        if len(args) > 1 and args[1].lower() in ["full", "完整", "详细"]:
            report_type = "full"
        logger.info(f"[AnalyzeCommand] Analyzing stock: {code}, report type: {report_type}")
        
        try:
            # 调用分析服务
            from src.services.task_service import get_task_service
            from src.enums import ReportType
            
            service = get_task_service()
            
            # 提交异步分析任务
            result = service.submit_analysis(
                code=code,
                report_type=ReportType.from_str(report_type),
                source_message=message
            )
            
            if result.get("success"):
                task_id = result.get("task_id", "")
                return BotResponse.markdown_response(
                    f"✅ **Analysis task submitted**\n\n"
                    f"• Stock code: `{code}`\n"
                    f"• Report type: {ReportType.from_str(report_type).display_name}\n"
                    f"• Task ID: `{task_id[:20]}...`\n\n"
                    f"Results will be sent automatically when the analysis completes."
                )
            else:
                error = result.get("error", "Unknown error")
                return BotResponse.error_response(f"Failed to submit analysis task: {error}")
                
        except Exception as e:
            logger.error(f"[AnalyzeCommand] Execution failed: {e}")
            return BotResponse.error_response(f"Analysis failed: {str(e)[:100]}")
