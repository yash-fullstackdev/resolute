"""
ConditionEvaluator — evaluates strategy conditions against current indicator
values.

Handles all operator types including crossovers (CROSSES_ABOVE / CROSSES_BELOW)
by comparing current vs previous values.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from .indicators import IndicatorResult
from .models import Condition, ConditionOperator

log = structlog.get_logger(__name__)


class ConditionEvaluator:
    """Evaluates conditions against current indicator values.

    Used by ``CustomStrategyWorker`` to determine entry/exit.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        condition: Condition,
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> bool:
        """Evaluate a single ``Condition`` against indicator results.

        Returns ``True`` if the condition is satisfied.
        """
        try:
            left_curr, left_prev = self._resolve_operand(
                condition.left_operand,
                condition.left_field,
                indicator_results,
                current_price,
            )
            right_curr, right_prev = self._resolve_right(
                condition,
                indicator_results,
                current_price,
            )

            if left_curr is None or right_curr is None:
                return False

            return self._apply_operator(
                condition.operator,
                left_curr,
                left_prev,
                right_curr,
                right_prev,
                condition,
                indicator_results,
            )
        except Exception:
            log.exception(
                "condition_eval_error",
                left=condition.left_operand,
                operator=condition.operator.value,
                right=condition.right_operand,
            )
            return False

    def evaluate_group(
        self,
        conditions: list[Condition],
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> bool:
        """AND logic: all conditions in the group must be true."""
        if not conditions:
            return False
        return all(
            self.evaluate(c, indicator_results, current_price)
            for c in conditions
        )

    def evaluate_entry(
        self,
        entry_conditions: list[list[Condition]],
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> bool:
        """OR-of-AND-groups: any fully satisfied group triggers entry."""
        if not entry_conditions:
            return False
        return any(
            self.evaluate_group(group, indicator_results, current_price)
            for group in entry_conditions
        )

    def evaluate_exit(
        self,
        exit_conditions: list[Condition],
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> bool:
        """OR logic: any satisfied condition triggers exit."""
        if not exit_conditions:
            return False
        return any(
            self.evaluate(c, indicator_results, current_price)
            for c in exit_conditions
        )

    # ------------------------------------------------------------------
    # Operand resolution
    # ------------------------------------------------------------------

    def _resolve_operand(
        self,
        operand: str,
        field_name: str | None,
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> tuple[float | None, float | None]:
        """Resolve an operand to (current_value, previous_value).

        Handles special operands ``PRICE`` and ``VOLUME``, as well as
        indicator labels with optional sub-fields.
        """
        if operand == "PRICE":
            return current_price, current_price

        if operand == "VOLUME":
            # Volume is not directly available as a scalar here;
            # it would need to come from an indicator result labelled "VOLUME"
            result = indicator_results.get("VOLUME")
            if result is None:
                return None, None
            return self._extract_value(result.current_value, field_name), \
                   self._extract_value(result.previous_value, field_name)

        result = indicator_results.get(operand)
        if result is None:
            log.debug("operand_not_found", operand=operand)
            return None, None

        curr = self._extract_value(result.current_value, field_name)
        prev = self._extract_value(result.previous_value, field_name)
        return curr, prev

    def _resolve_right(
        self,
        condition: Condition,
        indicator_results: dict[str, IndicatorResult],
        current_price: float,
    ) -> tuple[float | None, float | None]:
        """Resolve the right-hand side of a condition."""
        # Literal numeric value
        if condition.right_value is not None:
            val = condition.right_value
            return val, val

        # Named operand (another indicator or PRICE)
        return self._resolve_operand(
            condition.right_operand,
            condition.right_field,
            indicator_results,
            current_price,
        )

    @staticmethod
    def _extract_value(value: float | dict[str, float], field_name: str | None) -> float | None:
        """Extract a scalar from a possibly-complex indicator value.

        If *field_name* is provided and *value* is a dict, returns
        ``value[field_name]``.  Otherwise returns *value* as a float.
        """
        if isinstance(value, dict):
            if field_name is not None:
                v = value.get(field_name)
                return float(v) if v is not None else None
            # If no field specified for a dict value, try common single-value keys
            for key in ("value", "adx", "k"):
                if key in value:
                    return float(value[key])
            return None

        if isinstance(value, (int, float)):
            if math.isnan(value):
                return None
            return float(value)

        return None

    # ------------------------------------------------------------------
    # Operator application
    # ------------------------------------------------------------------

    def _apply_operator(
        self,
        operator: ConditionOperator,
        left_curr: float,
        left_prev: float | None,
        right_curr: float,
        right_prev: float | None,
        condition: Condition,
        indicator_results: dict[str, IndicatorResult],
    ) -> bool:
        """Apply the condition operator to resolved values."""

        if operator == ConditionOperator.GT:
            return left_curr > right_curr

        if operator == ConditionOperator.GTE:
            return left_curr >= right_curr

        if operator == ConditionOperator.LT:
            return left_curr < right_curr

        if operator == ConditionOperator.LTE:
            return left_curr <= right_curr

        if operator == ConditionOperator.EQ:
            return math.isclose(left_curr, right_curr, rel_tol=1e-9)

        if operator == ConditionOperator.NEQ:
            return not math.isclose(left_curr, right_curr, rel_tol=1e-9)

        if operator == ConditionOperator.CROSSES_ABOVE:
            # Left was below right on previous bar and is now above
            if left_prev is None or right_prev is None:
                return False
            return left_prev <= right_prev and left_curr > right_curr

        if operator == ConditionOperator.CROSSES_BELOW:
            # Left was above right on previous bar and is now below
            if left_prev is None or right_prev is None:
                return False
            return left_prev >= right_prev and left_curr < right_curr

        if operator == ConditionOperator.TOUCHED:
            # Price touched (crossed through) the indicator level within the
            # current bar.  We check if the current value is very close to
            # the right value, or if current and previous straddle it.
            if left_prev is None:
                return False
            tolerance = abs(right_curr) * 0.002 if right_curr != 0 else 0.01
            # Close enough to the level
            if abs(left_curr - right_curr) <= tolerance:
                return True
            # Crossed through (was on other side in previous bar)
            if (left_prev <= right_curr <= left_curr) or (left_curr <= right_curr <= left_prev):
                return True
            return False

        if operator == ConditionOperator.BETWEEN:
            # Right operand is expected to encode two bounds.
            # Convention: right_value is the lower bound, and an additional
            # upper bound is stored in condition.right_field as a stringified
            # float, or the right_operand contains "lower,upper".
            lower = right_curr
            upper = lower
            if condition.right_field is not None:
                try:
                    upper = float(condition.right_field)
                except (ValueError, TypeError):
                    upper = lower
            elif "," in condition.right_operand:
                parts = condition.right_operand.split(",")
                try:
                    lower = float(parts[0].strip())
                    upper = float(parts[1].strip())
                except (ValueError, IndexError):
                    pass
            return lower <= left_curr <= upper

        if operator == ConditionOperator.INCREASING:
            return self._check_trend(
                condition.left_operand,
                condition.left_field,
                indicator_results,
                increasing=True,
                periods=int(right_curr) if right_curr else 3,
            )

        if operator == ConditionOperator.DECREASING:
            return self._check_trend(
                condition.left_operand,
                condition.left_field,
                indicator_results,
                increasing=False,
                periods=int(right_curr) if right_curr else 3,
            )

        log.warning("unknown_operator", operator=operator.value)
        return False

    # ------------------------------------------------------------------
    # Trend checking helper (INCREASING / DECREASING)
    # ------------------------------------------------------------------

    def _check_trend(
        self,
        operand: str,
        field_name: str | None,
        indicator_results: dict[str, IndicatorResult],
        increasing: bool,
        periods: int = 3,
    ) -> bool:
        """Check if the indicator has been monotonically increasing (or
        decreasing) for the last *periods* values."""
        result = indicator_results.get(operand)
        if result is None or not result.history:
            return False

        history = result.history
        # Extract field from history if values are dicts
        values: list[float] = []
        for h in history:
            v = self._extract_value(h, field_name) if isinstance(h, dict) else h
            if v is not None and not math.isnan(v):
                values.append(v)

        if len(values) < periods:
            return False

        recent = values[-periods:]
        if increasing:
            return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))
        else:
            return all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))
