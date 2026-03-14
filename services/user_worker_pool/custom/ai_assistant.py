"""
AIStrategyAssistant — uses Claude API (anthropic SDK) to help users build,
validate, and optimise custom option strategies.

Runs server-side; the user interacts via the dashboard chat or API endpoints.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import anthropic
import structlog

from ..capital_tier import CapitalTier
from .indicators import IndicatorConfig, IndicatorType
from .models import (
    AIReview,
    Condition,
    ConditionOperator,
    CustomStrategyDefinition,
    LegTemplate,
    SpreadConfig,
    StrategySuggestion,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Indicator catalogue context for Claude prompts
# ---------------------------------------------------------------------------

_INDICATOR_CATALOGUE = """
Available Indicators (IndicatorType enum values):

LAGGING / TREND:
  SMA(period) — Simple Moving Average
  EMA(period) — Exponential Moving Average
  WMA(period) — Weighted Moving Average
  DEMA(period) — Double EMA
  MACD(fast=12, slow=26, signal=9) — returns {line, signal, histogram}
  MACD_HISTOGRAM(fast, slow, signal) — MACD histogram only
  BOLLINGER_BANDS(period=20, num_std=2) — returns {upper, middle, lower, width}
  SUPERTREND(period=10, multiplier=3) — returns {value, direction}; direction 1=BUY, -1=SELL
  PARABOLIC_SAR(af_start=0.02, af_step=0.02, af_max=0.20) — returns {value, direction}
  ICHIMOKU(tenkan_period=9, kijun_period=26, senkou_b_period=52, displacement=26) — returns {tenkan_sen, kijun_sen, senkou_a, senkou_b, chikou_span}
  ADX(period=14) — returns {adx, plus_di, minus_di}; ADX > 25 = strong trend
  MOVING_AVG_RIBBON(periods=[8,13,21,34,55,89]) — returns {ema_8, ema_13, ...}

LEADING / OSCILLATORS:
  RSI(period=14) — 0-100 scale; <30 oversold, >70 overbought
  STOCHASTIC(k_period=14, d_period=3) — returns {k, d}; 0-100
  STOCHASTIC_RSI(rsi_period=14, stoch_period=14, k_smooth=3, d_smooth=3) — returns {k, d}
  CCI(period=20) — Commodity Channel Index
  WILLIAMS_R(period=14) — 0 to -100; <-80 oversold, >-20 overbought
  MFI(period=14) — Money Flow Index (volume-weighted RSI); 0-100
  ROC(period=12) — Rate of Change (%)
  MOMENTUM(period=10) — absolute price momentum

VOLUME:
  VWAP — Volume Weighted Average Price (intraday cumulative)
  OBV — On Balance Volume
  AD_LINE — Accumulation/Distribution Line

VOLATILITY:
  ATR(period=14) — Average True Range
  BOLLINGER_WIDTH(period=20, num_std=2) — Bollinger Band squeeze detection
  KELTNER_CHANNEL(ema_period=20, atr_period=10, multiplier=1.5) — returns {upper, middle, lower}
  DONCHIAN_CHANNEL(period=20) — returns {upper, middle, lower}
  INDIA_VIX — India VIX value

OPTIONS-SPECIFIC:
  IV_RANK — IV Rank (0-100)
  IV_PERCENTILE — IV Percentile (0-100)
  PCR_OI — Put-Call Ratio by Open Interest
  PCR_VOLUME — Put-Call Ratio by Volume
  MAX_PAIN — Max pain strike
  OI_CHANGE — returns {call_oi_change, put_oi_change, net_oi_change}
  CALL_OI_CHANGE — call OI change scalar
  PUT_OI_CHANGE — put OI change scalar
  IV_SKEW — put IV minus call IV at equidistant OTM strikes

Condition Operators:
  >, >=, <, <=, ==, !=
  CROSSES_ABOVE — left was below right on previous bar, now above
  CROSSES_BELOW — left was above right on previous bar, now below
  TOUCHED — price touched or crossed through indicator level
  BETWEEN — value is between two bounds
  INCREASING — value has been increasing for N periods
  DECREASING — value has been decreasing for N periods

Option Actions: BUY_CALL, BUY_PUT, SELL_CALL, SELL_PUT, STRADDLE, STRANGLE, SPREAD
Strike Selection: ATM, 1_OTM, 2_OTM, 1_ITM, DELTA_BASED
"""

_TIER_GUIDELINES = {
    CapitalTier.STARTER: {
        "max_risk_pct": 2.0,
        "allowed_categories": ["BUYING"],
        "default_stop_loss_pct": 30.0,
        "default_target_pct": 60.0,
    },
    CapitalTier.GROWTH: {
        "max_risk_pct": 3.0,
        "allowed_categories": ["BUYING", "HYBRID"],
        "default_stop_loss_pct": 25.0,
        "default_target_pct": 50.0,
    },
    CapitalTier.PRO: {
        "max_risk_pct": 4.0,
        "allowed_categories": ["BUYING", "HYBRID", "SELLING"],
        "default_stop_loss_pct": 20.0,
        "default_target_pct": 40.0,
    },
    CapitalTier.INSTITUTIONAL: {
        "max_risk_pct": 5.0,
        "allowed_categories": ["BUYING", "HYBRID", "SELLING"],
        "default_stop_loss_pct": 15.0,
        "default_target_pct": 35.0,
    },
}


class AIStrategyAssistant:
    """Uses Claude API to help users build, validate, and optimise custom
    strategies.

    Requires an ``ANTHROPIC_API_KEY`` environment variable or explicit key.
    """

    MODEL = "claude-sonnet-4-20250514"

    def __init__(self, api_key: str | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    # ------------------------------------------------------------------
    # Build from natural language
    # ------------------------------------------------------------------

    async def build_from_description(
        self,
        user_description: str,
        user_tier: CapitalTier,
        preferred_segments: list[str] | None = None,
    ) -> CustomStrategyDefinition:
        """Convert a natural-language strategy description into a fully
        populated ``CustomStrategyDefinition``.

        The AI selects indicators, entry/exit conditions, strike selection,
        DTE range, and risk parameters appropriate for the user's capital
        tier.
        """
        tier_guide = _TIER_GUIDELINES.get(user_tier, _TIER_GUIDELINES[CapitalTier.STARTER])
        segments = preferred_segments or ["NSE_INDEX"]

        system_prompt = (
            "You are an expert Indian options strategy builder. "
            "Given a user's natural language description of a trading strategy, "
            "you must produce a JSON object representing a complete custom strategy definition.\n\n"
            f"INDICATOR LIBRARY:\n{_INDICATOR_CATALOGUE}\n\n"
            f"USER TIER: {user_tier.value}\n"
            f"TIER GUIDELINES: {json.dumps(tier_guide)}\n"
            f"PREFERRED SEGMENTS: {segments}\n\n"
            "OUTPUT FORMAT — return ONLY valid JSON with these exact keys:\n"
            "{\n"
            '  "name": "string — concise strategy name",\n'
            '  "description": "string — brief description",\n'
            '  "category": "BUYING | SELLING | HYBRID",\n'
            '  "target_symbols": ["NIFTY", ...],\n'
            '  "target_segments": ["NSE_INDEX", ...],\n'
            '  "indicators": [\n'
            '    {"indicator_type": "RSI", "params": {"period": 14}, "label": "RSI_14"},\n'
            "    ...\n"
            "  ],\n"
            '  "entry_conditions": [\n'
            "    [\n"
            '      {"left_operand": "RSI_14", "left_field": null, "operator": "<", "right_operand": "", "right_field": null, "right_value": 30},\n'
            "      ...\n"
            "    ]\n"
            "  ],\n"
            '  "exit_conditions": [\n'
            '    {"left_operand": "RSI_14", "left_field": null, "operator": ">", "right_operand": "", "right_field": null, "right_value": 70}\n'
            "  ],\n"
            '  "option_action": "BUY_CALL | BUY_PUT | SELL_CALL | SELL_PUT | STRADDLE | STRANGLE | SPREAD",\n'
            '  "strike_selection": "ATM | 1_OTM | 2_OTM | 1_ITM | DELTA_BASED",\n'
            '  "delta_target": null,\n'
            '  "dte_min": 7,\n'
            '  "dte_max": 14,\n'
            '  "spread_config": null,\n'
            '  "stop_loss_pct": 30.0,\n'
            '  "profit_target_pct": 60.0,\n'
            '  "time_stop_rule": "eod | fixed_dte_3 | custom_time",\n'
            '  "time_stop_value": null,\n'
            '  "max_positions_per_symbol": 1\n'
            "}\n\n"
            "RULES:\n"
            f"1. Only use categories allowed for this tier: {tier_guide['allowed_categories']}\n"
            "2. Always include sensible exit conditions (profit target, stop loss indicator signals)\n"
            "3. Use at least one leading AND one lagging indicator for confirmation\n"
            "4. Do NOT over-fit — keep conditions to 2-5 per entry group\n"
            "5. Set appropriate DTE range for the strategy type (weekly for scalps, monthly for swing)\n"
            "6. Return ONLY the JSON, no markdown, no explanation.\n"
        )

        log.info("ai_build_from_description", description=user_description[:100])

        response = self.client.messages.create(
            model=self.MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_description}],
        )

        raw_text = response.content[0].text.strip()
        strategy_dict = self._parse_json_response(raw_text)

        return self._dict_to_strategy(strategy_dict, user_tier)

    # ------------------------------------------------------------------
    # Review strategy
    # ------------------------------------------------------------------

    async def review_strategy(
        self,
        strategy: CustomStrategyDefinition,
        backtest_results: dict[str, Any] | None = None,
    ) -> AIReview:
        """AI reviews the strategy for logical consistency, risk, overfitting,
        and regime suitability."""

        strategy_json = self._strategy_to_summary(strategy)

        system_prompt = (
            "You are an expert Indian options trading risk analyst. "
            "Review the following custom strategy and provide a structured analysis.\n\n"
            f"INDICATOR LIBRARY:\n{_INDICATOR_CATALOGUE}\n\n"
            "OUTPUT FORMAT — return ONLY valid JSON:\n"
            "{\n"
            '  "overall_rating": "STRONG | MODERATE | WEAK | RISKY",\n'
            '  "risk_score": 0-100,\n'
            '  "issues": ["issue 1", ...],\n'
            '  "suggestions": ["suggestion 1", ...],\n'
            '  "overfitting_risk": "LOW | MEDIUM | HIGH",\n'
            '  "regime_coverage": {\n'
            '    "BULL_LOW_VOL": "good | moderate | poor | untested",\n'
            '    "BULL_HIGH_VOL": "...",\n'
            '    "BEAR_LOW_VOL": "...",\n'
            '    "BEAR_HIGH_VOL": "...",\n'
            '    "SIDEWAYS": "..."\n'
            "  }\n"
            "}\n\n"
            "REVIEW CRITERIA:\n"
            "1. Logical consistency — are there conflicting conditions or impossible states?\n"
            "2. Risk — is stop loss appropriate? Is position sizing safe for the category?\n"
            "3. Overfitting — too many conditions (>5 per group) = HIGH overfitting risk\n"
            "4. Regime suitability — will it work in trending AND ranging markets?\n"
            "5. Exit quality — are there sufficient exit conditions?\n"
            "6. For SELLING strategies: is margin utilization reasonable?\n"
            "Return ONLY the JSON, no markdown, no explanation.\n"
        )

        backtest_context = ""
        if backtest_results:
            backtest_context = f"\n\nBACKTEST RESULTS:\n{json.dumps(backtest_results, default=str)}"

        log.info("ai_review_strategy", strategy=strategy.name)

        response = self.client.messages.create(
            model=self.MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"STRATEGY:\n{strategy_json}{backtest_context}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        review_dict = self._parse_json_response(raw_text)

        return AIReview(
            overall_rating=review_dict.get("overall_rating", "MODERATE"),
            risk_score=float(review_dict.get("risk_score", 50)),
            issues=review_dict.get("issues", []),
            suggestions=review_dict.get("suggestions", []),
            overfitting_risk=review_dict.get("overfitting_risk", "MEDIUM"),
            regime_coverage=review_dict.get("regime_coverage", {}),
        )

    # ------------------------------------------------------------------
    # Suggest improvements
    # ------------------------------------------------------------------

    async def suggest_improvements(
        self,
        strategy: CustomStrategyDefinition,
        backtest_results: dict[str, Any],
    ) -> list[StrategySuggestion]:
        """Based on backtest results, suggest parameter tweaks and structural
        changes to improve the strategy."""

        strategy_json = self._strategy_to_summary(strategy)

        system_prompt = (
            "You are an expert options strategy optimiser. "
            "Based on the strategy definition and backtest results, suggest specific improvements.\n\n"
            f"INDICATOR LIBRARY:\n{_INDICATOR_CATALOGUE}\n\n"
            "OUTPUT FORMAT — return ONLY a JSON array:\n"
            "[\n"
            "  {\n"
            '    "change_type": "PARAM_TWEAK | ADD_CONDITION | REMOVE_CONDITION | SYMBOL_FOCUS",\n'
            '    "description": "Specific actionable change",\n'
            '    "expected_improvement": "Win rate +5%",\n'
            '    "confidence": 0.0-1.0\n'
            "  },\n"
            "  ...\n"
            "]\n\n"
            "GUIDELINES:\n"
            "1. Suggest 3-7 improvements ranked by expected impact\n"
            "2. Be specific — mention exact parameter values\n"
            "3. Consider adding/removing indicators to reduce overfitting or improve accuracy\n"
            "4. If certain symbols outperform others, suggest focusing\n"
            "5. If win rate is low, suggest tighter filters; if profit factor is low, suggest wider targets\n"
            "Return ONLY the JSON array, no markdown, no explanation.\n"
        )

        log.info("ai_suggest_improvements", strategy=strategy.name)

        response = self.client.messages.create(
            model=self.MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"STRATEGY:\n{strategy_json}\n\n"
                        f"BACKTEST RESULTS:\n{json.dumps(backtest_results, default=str)}"
                    ),
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        suggestions_list = self._parse_json_response(raw_text)

        if not isinstance(suggestions_list, list):
            suggestions_list = [suggestions_list]

        result: list[StrategySuggestion] = []
        for item in suggestions_list:
            if not isinstance(item, dict):
                continue
            result.append(StrategySuggestion(
                change_type=item.get("change_type", "PARAM_TWEAK"),
                description=item.get("description", ""),
                expected_improvement=item.get("expected_improvement", ""),
                confidence=float(item.get("confidence", 0.5)),
            ))

        return result

    # ------------------------------------------------------------------
    # JSON parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_response(text: str) -> Any:
        """Parse a JSON response from Claude, stripping markdown fences if
        present."""
        # Strip markdown code fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first line (```json or ```)
            lines = lines[1:]
            # Remove last line if it's ```)
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            log.error("ai_json_parse_error", raw=text[:200])
            return {}

    # ------------------------------------------------------------------
    # Strategy serialisation helpers
    # ------------------------------------------------------------------

    def _strategy_to_summary(self, strategy: CustomStrategyDefinition) -> str:
        """Convert a strategy to a readable JSON summary for Claude prompts."""
        indicators_summary = []
        for ind in strategy.indicators:
            indicators_summary.append({
                "type": ind.indicator_type.value if isinstance(ind.indicator_type, IndicatorType) else str(ind.indicator_type),
                "params": ind.params,
                "label": ind.label,
            })

        conditions_summary = []
        for group in strategy.entry_conditions:
            group_list = []
            for c in group:
                group_list.append({
                    "left": c.left_operand,
                    "left_field": c.left_field,
                    "op": c.operator.value if isinstance(c.operator, ConditionOperator) else str(c.operator),
                    "right": c.right_operand,
                    "right_field": c.right_field,
                    "right_value": c.right_value,
                })
            conditions_summary.append(group_list)

        exit_summary = []
        for c in strategy.exit_conditions:
            exit_summary.append({
                "left": c.left_operand,
                "left_field": c.left_field,
                "op": c.operator.value if isinstance(c.operator, ConditionOperator) else str(c.operator),
                "right": c.right_operand,
                "right_field": c.right_field,
                "right_value": c.right_value,
            })

        summary = {
            "name": strategy.name,
            "description": strategy.description,
            "category": strategy.category,
            "target_symbols": strategy.target_symbols,
            "target_segments": strategy.target_segments,
            "indicators": indicators_summary,
            "entry_conditions": conditions_summary,
            "exit_conditions": exit_summary,
            "option_action": strategy.option_action,
            "strike_selection": strategy.strike_selection,
            "delta_target": strategy.delta_target,
            "dte_min": strategy.dte_min,
            "dte_max": strategy.dte_max,
            "stop_loss_pct": strategy.stop_loss_pct,
            "profit_target_pct": strategy.profit_target_pct,
            "time_stop_rule": strategy.time_stop_rule,
            "max_positions_per_symbol": strategy.max_positions_per_symbol,
        }

        return json.dumps(summary, indent=2, default=str)

    def _dict_to_strategy(
        self,
        d: dict[str, Any],
        user_tier: CapitalTier,
    ) -> CustomStrategyDefinition:
        """Convert an AI-generated dict into a ``CustomStrategyDefinition``."""

        # Parse indicators
        indicators: list[IndicatorConfig] = []
        for ind_dict in d.get("indicators", []):
            try:
                itype = IndicatorType(ind_dict["indicator_type"])
            except (ValueError, KeyError):
                log.warning("unknown_indicator_in_ai_response", raw=ind_dict)
                continue
            indicators.append(IndicatorConfig(
                indicator_type=itype,
                params=ind_dict.get("params", {}),
                label=ind_dict.get("label", ""),
            ))

        # Parse entry conditions (list of list of Condition)
        entry_conditions: list[list[Condition]] = []
        for group in d.get("entry_conditions", []):
            parsed_group: list[Condition] = []
            for cond_dict in group:
                parsed_group.append(self._parse_condition(cond_dict))
            if parsed_group:
                entry_conditions.append(parsed_group)

        # Parse exit conditions (flat list)
        exit_conditions: list[Condition] = []
        for cond_dict in d.get("exit_conditions", []):
            exit_conditions.append(self._parse_condition(cond_dict))

        # Parse spread config
        spread_config = None
        sc = d.get("spread_config")
        if sc and isinstance(sc, dict):
            legs = []
            for leg in sc.get("legs", []):
                legs.append(LegTemplate(
                    action=leg.get("action", "BUY"),
                    option_type=leg.get("option_type", "CE"),
                    strike_offset=int(leg.get("strike_offset", 0)),
                    quantity_ratio=int(leg.get("quantity_ratio", 1)),
                ))
            spread_config = SpreadConfig(legs=legs)

        # Apply tier defaults where not specified
        tier_guide = _TIER_GUIDELINES.get(user_tier, _TIER_GUIDELINES[CapitalTier.STARTER])

        return CustomStrategyDefinition(
            id=str(uuid.uuid4()),
            tenant_id="",  # Set by caller
            name=d.get("name", "AI Strategy"),
            description=d.get("description", ""),
            category=d.get("category", "BUYING"),
            status="DRAFT",
            target_symbols=d.get("target_symbols", ["NIFTY"]),
            target_segments=d.get("target_segments", ["NSE_INDEX"]),
            indicators=indicators,
            entry_conditions=entry_conditions,
            exit_conditions=exit_conditions,
            option_action=d.get("option_action", "BUY_CALL"),
            strike_selection=d.get("strike_selection", "ATM"),
            delta_target=d.get("delta_target"),
            dte_min=int(d.get("dte_min", 7)),
            dte_max=int(d.get("dte_max", 14)),
            spread_config=spread_config,
            stop_loss_pct=float(d.get("stop_loss_pct", tier_guide["default_stop_loss_pct"])),
            profit_target_pct=float(d.get("profit_target_pct", tier_guide["default_target_pct"])),
            time_stop_rule=d.get("time_stop_rule", "eod"),
            time_stop_value=d.get("time_stop_value"),
            max_positions_per_symbol=int(d.get("max_positions_per_symbol", 1)),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

    @staticmethod
    def _parse_condition(d: dict[str, Any]) -> Condition:
        """Parse a condition dict from AI output into a ``Condition``."""
        # Map operator string to enum
        op_str = d.get("operator", ">")
        try:
            operator = ConditionOperator(op_str)
        except ValueError:
            # Try matching by name
            op_upper = op_str.upper().replace(" ", "_")
            try:
                operator = ConditionOperator[op_upper]
            except KeyError:
                operator = ConditionOperator.GT

        return Condition(
            left_operand=d.get("left_operand", ""),
            left_field=d.get("left_field"),
            operator=operator,
            right_operand=d.get("right_operand", ""),
            right_field=d.get("right_field"),
            right_value=d.get("right_value"),
        )
