# -*- coding: utf-8 -*-
"""
===================================
批量分析命令
===================================

批量分析自选股列表中的所有股票。
"""

import logging
import threading
import uuid
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)


class BatchCommand(BotCommand):
    """
    批量分析命令
    
    批量分析配置中的自选股列表，生成汇总报告。
    
    用法：
        /batch      - 分析所有自选股
        /batch 3    - 只分析前3只
    """
    
    @property
    def name(self) -> str:
        return "batch"
    
    @property
    def aliases(self) -> List[str]:
        return ["b", "批量", "全部"]
    
    @property
    def description(self) -> str:
        return "Analyze all stocks in your watchlist"
    
    @property
    def usage(self) -> str:
        return "/batch [count]"
    
    @property
    def admin_only(self) -> bool:
        """批量分析需要管理员权限（防止滥用）"""
        return False  # 可以根据需要设为 True
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行批量分析命令"""
        from src.config import get_config
        
        config = get_config()
        config.refresh_stock_list()
        
        stock_list = config.stock_list
        
        if not stock_list:
            return BotResponse.error_response(
                "Watchlist is empty. Please configure STOCK_LIST first."
            )
        
        # 解析数量参数
        limit = None
        if args:
            try:
                limit = int(args[0])
                if limit <= 0:
                    return BotResponse.error_response("Count must be greater than 0")
            except ValueError:
                return BotResponse.error_response(f"Invalid count: {args[0]}")
        
        # 限制分析数量
        if limit:
            stock_list = stock_list[:limit]
        
        logger.info(f"[BatchCommand] Starting batch analysis of {len(stock_list)} stocks")
        
        # 在后台线程中执行分析
        thread = threading.Thread(
            target=self._run_batch_analysis,
            args=(stock_list, message),
            daemon=True
        )
        thread.start()
        
        return BotResponse.markdown_response(
            f"✅ **Batch analysis started**\n\n"
            f"• Stocks to analyze: {len(stock_list)}\n"
            f"• Stock list: {', '.join(stock_list[:5])}"
            f"{'...' if len(stock_list) > 5 else ''}\n\n"
            f"A summary report will be sent automatically when the analysis completes."
        )
    
    def _run_batch_analysis(self, stock_list: List[str], message: BotMessage) -> None:
        """后台执行批量分析"""
        try:
            from src.config import get_config
            from main import StockAnalysisPipeline
            
            config = get_config()
            
            # 创建分析管道
            pipeline = StockAnalysisPipeline(
                config=config,
                source_message=message,
                query_id=uuid.uuid4().hex,
                query_source="bot"
            )
            
            # 执行分析（会自动推送汇总报告）
            results = pipeline.run(
                stock_codes=stock_list,
                dry_run=False,
                send_notification=True
            )
            
            logger.info(f"[BatchCommand] Batch analysis complete, {len(results)} succeeded")
            
        except Exception as e:
            logger.error(f"[BatchCommand] Batch analysis failed: {e}")
            logger.exception(e)
