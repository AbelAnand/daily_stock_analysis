# -*- coding: utf-8 -*-
"""
每日分析后的结算钩子 + 推送战绩区块 + Agent 层风险消费 测试

覆盖：
1. main._run_outcome_scoring：决策信号后验评估 + 回测摘要刷新的调用/跳过/容错语义；
2. run_full_analysis 在分析完成后调用结算钩子；
3. NotificationService 战绩区块：有汇总时渲染、无汇总时静默跳过；
4. Orchestrator 消费 risk_context.position_size_factor 与 stop_tightening；
5. StrategyEngine 共识 opinion 携带分布决策元数据与 risk_context（与聚合器统一）。
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for optional_module in ("litellm", "json_repair"):
    try:
        __import__(optional_module)
    except ModuleNotFoundError:
        sys.modules[optional_module] = mock.MagicMock()

import main
from src.config import Config


class OutcomeScoringHookTest(unittest.TestCase):
    """main._run_outcome_scoring 的调用语义。"""

    def test_disabled_flag_skips_everything(self):
        config = SimpleNamespace(outcome_scoring_enabled=False, backtest_enabled=False)
        with patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
        ) as outcome_cls, patch(
            "src.services.backtest_service.BacktestService"
        ) as backtest_cls:
            main._run_outcome_scoring(config)
        outcome_cls.assert_not_called()
        backtest_cls.assert_not_called()

    def test_enabled_runs_outcomes_and_backtest_refresh(self):
        config = SimpleNamespace(
            outcome_scoring_enabled=True,
            backtest_enabled=False,
            backtest_eval_window_days=10,
            backtest_min_age_days=14,
        )
        with patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
        ) as outcome_cls, patch(
            "src.services.backtest_service.BacktestService"
        ) as backtest_cls:
            outcome_cls.return_value.run_outcomes.return_value = {
                "evaluated": 1, "created": 1, "updated": 0, "skipped": 0,
                "engine_version": "signal_outcome_v1",
            }
            backtest_cls.return_value.run_backtest.return_value = {
                "processed": 1, "saved": 1, "completed": 1, "insufficient": 0, "errors": 0,
            }
            main._run_outcome_scoring(config)
        outcome_cls.return_value.run_outcomes.assert_called_once_with(limit=200)
        backtest_cls.return_value.run_backtest.assert_called_once_with(
            force=False, eval_window_days=10, min_age_days=14, limit=200,
        )

    def test_backtest_enabled_skips_duplicate_refresh(self):
        """backtest_enabled 时 _run_auto_backtest 已跑过回测，避免重复。"""
        config = SimpleNamespace(
            outcome_scoring_enabled=True,
            backtest_enabled=True,
        )
        with patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
        ) as outcome_cls, patch(
            "src.services.backtest_service.BacktestService"
        ) as backtest_cls:
            outcome_cls.return_value.run_outcomes.return_value = {}
            main._run_outcome_scoring(config)
        outcome_cls.return_value.run_outcomes.assert_called_once()
        backtest_cls.assert_not_called()

    def test_outcome_failure_never_raises_and_backtest_still_runs(self):
        config = SimpleNamespace(
            outcome_scoring_enabled=True,
            backtest_enabled=False,
            backtest_eval_window_days=10,
            backtest_min_age_days=14,
        )
        with patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
        ) as outcome_cls, patch(
            "src.services.backtest_service.BacktestService"
        ) as backtest_cls:
            outcome_cls.return_value.run_outcomes.side_effect = RuntimeError("db locked")
            backtest_cls.return_value.run_backtest.side_effect = RuntimeError("boom")
            main._run_outcome_scoring(config)  # must not raise
        outcome_cls.return_value.run_outcomes.assert_called_once()
        backtest_cls.return_value.run_backtest.assert_called_once()

    def test_run_full_analysis_invokes_outcome_scoring(self):
        """空 Futu 持仓 + 关闭大盘复盘的早退路径也要触发结算钩子。"""
        args = SimpleNamespace(portfolio="futu", no_market_review=True)
        config = SimpleNamespace(market_review_enabled=False)
        with patch(
            "src.brokers.futu.portfolio.load_futu_stock_codes",
            return_value=[],
        ), patch.object(main, "_run_auto_backtest") as auto_backtest, patch.object(
            main, "_run_outcome_scoring"
        ) as outcome_scoring:
            result = main.run_full_analysis(config, args)
        self.assertTrue(result)
        auto_backtest.assert_called_once_with(config)
        outcome_scoring.assert_called_once_with(config)

    def test_config_defaults(self):
        config = Config(stock_list=[])
        self.assertTrue(config.outcome_scoring_enabled)
        self.assertEqual(config.backtest_engine_version, "v2")


class ScorecardSectionTest(unittest.TestCase):
    """NotificationService 战绩区块渲染。"""

    SUMMARY = {
        "eval_window_days": 10,
        "strict_accuracy_pct": 54.2,
        "win_rate_pct": 61.5,
        "scored_count": 120,
        "calibration": {"brier_score": 0.231, "sample_count": 98},
        "direction_breakdown": {
            "up": {"strict_accuracy_pct": 62.0, "total": 40},
            "not_down": {"strict_accuracy_pct": 55.0, "total": 30},
            "flat": {"win_rate_pct": 48.0, "total": 20},
            "down": {"strict_accuracy_pct": 50.0, "total": 10},
        },
    }

    def _make_service(self):
        from src.notification import NotificationService

        with mock.patch("src.notification.get_config", return_value=Config(stock_list=[])):
            return NotificationService()

    def test_scorecard_rendered_when_summary_exists(self):
        service = self._make_service()
        with patch("src.services.backtest_service.BacktestService") as backtest_cls:
            backtest_cls.return_value.get_summary.return_value = dict(self.SUMMARY)
            section = service._build_scorecard_section()
        self.assertIn("📊 战绩", section)
        self.assertIn("54.2%", section)
        self.assertIn("61.5%", section)
        self.assertIn("n=120", section)
        self.assertIn("0.231", section)
        self.assertIn("买 62.0%", section)
        self.assertIn("卖 50.0%", section)
        # 手机端摘要：控制在 6 行以内
        self.assertLessEqual(len(section.splitlines()), 6)
        backtest_cls.return_value.get_summary.assert_called_once_with(
            scope="overall", code=None, eval_window_days=None,
        )

    def test_scorecard_silently_absent_without_summary(self):
        service = self._make_service()
        with patch("src.services.backtest_service.BacktestService") as backtest_cls:
            backtest_cls.return_value.get_summary.return_value = None
            self.assertEqual(service._build_scorecard_section(), "")

    def test_scorecard_silently_absent_on_read_error(self):
        service = self._make_service()
        with patch("src.services.backtest_service.BacktestService") as backtest_cls:
            backtest_cls.return_value.get_summary.side_effect = RuntimeError("no db")
            self.assertEqual(service._build_scorecard_section(), "")

    def test_scorecard_absent_when_metrics_all_missing(self):
        """旧汇总行（无 metrics_ext、无 win_rate）不渲染半空区块。"""
        service = self._make_service()
        with patch("src.services.backtest_service.BacktestService") as backtest_cls:
            backtest_cls.return_value.get_summary.return_value = {
                "eval_window_days": 10,
                "strict_accuracy_pct": None,
                "win_rate_pct": None,
            }
            self.assertEqual(service._build_scorecard_section(), "")

    def test_aggregate_report_appends_scorecard(self):
        service = self._make_service()
        with mock.patch.object(
            service, "generate_dashboard_report", return_value="BASE"
        ), mock.patch.object(
            service, "_build_scorecard_section", return_value="📊 战绩\n- x"
        ):
            report = service.generate_aggregate_report([], "dashboard")
        self.assertEqual(report, "BASE\n\n📊 战绩\n- x")

    def test_aggregate_report_unchanged_without_scorecard(self):
        service = self._make_service()
        with mock.patch.object(
            service, "generate_dashboard_report", return_value="BASE"
        ), mock.patch.object(
            service, "_build_scorecard_section", return_value=""
        ):
            report = service.generate_aggregate_report([], "dashboard")
        self.assertEqual(report, "BASE")


class OrchestratorRiskConsumptionTest(unittest.TestCase):
    """Orchestrator 消费非方向性风险输出。"""

    def _make_orchestrator(self):
        from src.agent.orchestrator import AgentOrchestrator

        return AgentOrchestrator(
            tool_registry=MagicMock(),
            llm_adapter=MagicMock(),
            config=SimpleNamespace(agent_risk_override=True),
        )

    @staticmethod
    def _risk_opinion(factor=0.6, risk_level="medium"):
        from src.agent.protocols import AgentOpinion

        return AgentOpinion(
            agent_name="risk",
            signal="risk_assessment",
            confidence=0.6,
            reasoning="risk assessment",
            raw_data={
                "risk_level": risk_level,
                "veto_buy": False,
                "position_size_factor": factor,
                "risk_notes": ["业绩预亏"],
                "severe_flags": [],
            },
        )

    def test_default_position_size_applies_factor(self):
        from src.agent.orchestrator import _default_position_size

        self.assertEqual(_default_position_size("buy"), "轻仓试仓")
        self.assertEqual(_default_position_size("buy", 1.0), "轻仓试仓")
        self.assertIn("0.60", _default_position_size("buy", 0.6))
        self.assertIn("轻仓试仓", _default_position_size("buy", 0.6))
        self.assertIn("暂不建仓", _default_position_size("buy", 0.0))

    def test_finalize_scales_position_and_appends_stop_tightening(self):
        from src.agent.protocols import AgentContext
        from src.agent.risk_override import build_risk_override_plan

        orch = self._make_orchestrator()
        ctx = AgentContext(stock_code="600519", stock_name="贵州茅台")
        ctx.opinions.append(self._risk_opinion(factor=0.6, risk_level="medium"))
        plan = build_risk_override_plan(ctx, current_signal="buy", override_enabled=True)
        self.assertTrue(plan.stop_tightening_required)
        ctx.meta["risk_override_plan"] = plan

        payload = {
            "decision_type": "buy",
            "dashboard": {
                "battle_plan": {
                    "position_strategy": {
                        "suggested_position": "五成仓",
                        "entry_plan": "回踩买入",
                        "risk_control": "止损 95",
                    }
                }
            },
        }
        result = orch._finalize_dashboard_payload(payload, ctx)
        strategy = result["dashboard"]["battle_plan"]["position_strategy"]
        self.assertIn("五成仓", strategy["suggested_position"])
        self.assertIn("0.60", strategy["suggested_position"])
        self.assertIn("止损 95", strategy["risk_control"])
        self.assertIn("收紧止损", strategy["risk_control"])

    def test_finalize_surfaces_hold_reason_and_decision_rule(self):
        from src.agent.protocols import AgentContext

        orch = self._make_orchestrator()
        ctx = AgentContext(stock_code="600519", stock_name="贵州茅台")
        hold_reason = "hold due to disagreement: bullish=['trend'] vs bearish=['chip']"
        ctx.set_data(
            "skill_consensus",
            {
                "signal": "hold",
                "confidence": 0.5,
                "reasoning": "consensus",
                "raw_data": {
                    "decision_rule": "divergence_hold",
                    "decision_reason": hold_reason,
                    "hold_reason": hold_reason,
                },
            },
        )
        result = orch._finalize_dashboard_payload({"decision_type": "hold"}, ctx)
        consensus_decision = result["dashboard"]["consensus_decision"]
        self.assertEqual(consensus_decision["decision_rule"], "divergence_hold")
        self.assertEqual(consensus_decision["hold_reason"], hold_reason)
        self.assertTrue(
            any("hold 原因" in str(point) for point in result["key_points"]),
            result["key_points"],
        )


class EngineConsensusUnificationTest(unittest.TestCase):
    """StrategyEngine 与 SkillAggregator 的共识构建统一。"""

    @staticmethod
    def _skill_opinion(name, signal, confidence=0.7):
        from src.agent.protocols import AgentOpinion

        return AgentOpinion(
            agent_name=f"skill_{name}",
            signal=signal,
            confidence=confidence,
            reasoning=f"{name} view",
            raw_data={"signal": signal},
        )

    def test_engine_consensus_carries_distribution_fields_and_risk_context(self):
        from src.agent.skills.engine import StrategyEngine, StrategyResultStatus
        from src.agent.protocols import AgentOpinion

        risk_opinion = AgentOpinion(
            agent_name="risk",
            signal="risk_assessment",
            confidence=0.6,
            reasoning="risk",
            raw_data={
                "risk_level": "medium",
                "veto_buy": False,
                "position_size_factor": 0.6,
                "risk_notes": ["减持计划"],
            },
        )
        opinions = [
            self._skill_opinion("trend", "buy"),
            self._skill_opinion("chip", "sell"),
            risk_opinion,
        ]
        result = StrategyEngine().process(opinions)
        self.assertEqual(result.status, StrategyResultStatus.CONSENSUS)
        raw = result.consensus_opinion.raw_data
        # 分布决策元数据必须存活到共识 opinion
        self.assertEqual(raw["decision_rule"], "divergence_hold")
        self.assertIn("signal_distribution", raw)
        self.assertIn("direction_counts", raw)
        self.assertIn("hold_reason", raw)
        # 非方向性 risk_context 附着在共识上（与 aggregate() 路径一致）
        self.assertEqual(raw["risk_context"]["position_size_factor"], 0.6)
        self.assertFalse(raw["risk_context"]["severe_veto"])


if __name__ == "__main__":
    unittest.main()
