"""
AI Token Usage & Operational Cost Accounting.
Calculates token usage across models, estimates cost in INR (₹),
and computes net trading profits after deducting AI costs.
"""

from typing import Dict, Any, Optional
from app.storage.db import db

# Pricing estimates per 1,000,000 tokens (USD)
# USD to INR exchange rate estimated at ~84.5 INR
USD_TO_INR = 84.50
# User configured institutional capacity: 10M input tokens, 500K output tokens per day
MAX_DAILY_INPUT_TOKENS = 10_000_000
MAX_DAILY_OUTPUT_TOKENS = 500_000
MAX_DAILY_TOKENS = MAX_DAILY_INPUT_TOKENS + MAX_DAILY_OUTPUT_TOKENS  # 10.5M total

MODEL_PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-3.8-flash": {"input": 0.15, "output": 0.60},
    "gemini-4-argon": {"input": 0.20, "output": 0.80},
    "deepseek-ai/deepseek-v4.1-flash-maas": {"input": 0.14, "output": 0.28},
    "claude-opus-4-6-thinking": {"input": 0.0, "output": 0.0},
    "claude-sonnet-4-6": {"input": 0.0, "output": 0.0},
}


class TokenTracker:
    """Tracks token consumption, enforces 10M input / 500K output daily budget, and calculates AI costs in INR."""

    def can_consume_tokens(
        self,
        estimated_prompt_tokens: int = 50_000,
        estimated_completion_tokens: Optional[int] = None,
        is_emergency: bool = False
    ) -> bool:
        """
        Enforce daily token caps: 10,000,000 input tokens and 500,000 output tokens.
        Allows backward-compatible single argument calls.
        """
        prompt_est = estimated_prompt_tokens
        comp_est = 500 if estimated_completion_tokens is None else estimated_completion_tokens

        breakdown = db.get_today_token_breakdown()
        today_prompt = breakdown["prompt_tokens"]
        today_comp = breakdown["completion_tokens"]

        prompt_cap = MAX_DAILY_INPUT_TOKENS * 1.15 if is_emergency else MAX_DAILY_INPUT_TOKENS
        comp_cap = MAX_DAILY_OUTPUT_TOKENS * 1.15 if is_emergency else MAX_DAILY_OUTPUT_TOKENS

        if (today_prompt + prompt_est) > prompt_cap:
            return False
        if (today_comp + comp_est) > comp_cap:
            return False
        return True

    def get_budget_status(self) -> Dict[str, Any]:
        """Get daily 10M input / 500K output quota consumption status."""
        breakdown = db.get_today_token_breakdown()
        today_prompt = breakdown["prompt_tokens"]
        today_comp = breakdown["completion_tokens"]
        today_total = breakdown["total_tokens"]

        remaining_input = max(0, MAX_DAILY_INPUT_TOKENS - today_prompt)
        remaining_output = max(0, MAX_DAILY_OUTPUT_TOKENS - today_comp)
        remaining_total = max(0, MAX_DAILY_TOKENS - today_total)

        used_input_pct = round((today_prompt / MAX_DAILY_INPUT_TOKENS) * 100, 2)
        used_output_pct = round((today_comp / MAX_DAILY_OUTPUT_TOKENS) * 100, 2)
        used_total_pct = round((today_total / MAX_DAILY_TOKENS) * 100, 2)

        is_throttled = (today_prompt >= MAX_DAILY_INPUT_TOKENS) or (today_comp >= MAX_DAILY_OUTPUT_TOKENS)

        return {
            "max_daily_tokens": MAX_DAILY_TOKENS,
            "max_daily_input_tokens": MAX_DAILY_INPUT_TOKENS,
            "max_daily_output_tokens": MAX_DAILY_OUTPUT_TOKENS,
            "today_tokens_used": today_total,
            "today_prompt_tokens": today_prompt,
            "today_completion_tokens": today_comp,
            "remaining_daily_tokens": remaining_total,
            "remaining_input_tokens": remaining_input,
            "remaining_output_tokens": remaining_output,
            "daily_budget_used_pct": min(used_total_pct, 100.0),
            "input_budget_used_pct": min(used_input_pct, 100.0),
            "output_budget_used_pct": min(used_output_pct, 100.0),
            "is_throttled": is_throttled,
        }

    def calculate_cost_inr(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Compute estimated cost in Indian Rupees."""
        rates = MODEL_PRICING.get(model.lower(), {"input": 0.14, "output": 0.28})
        cost_usd = ((prompt_tokens / 1_000_000) * rates["input"]) + ((completion_tokens / 1_000_000) * rates["output"])
        return round(cost_usd * USD_TO_INR, 4)

    def log_usage(self, module: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Record token consumption in database."""
        cost = self.calculate_cost_inr(model, prompt_tokens, completion_tokens)
        db.record_token_usage(
            module=module,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_inr=cost
        )
        return cost

    def get_summary(self, realized_pnl: float = 0.0) -> Dict[str, Any]:
        """Get total tokens, cost, daily quota, and net profit after AI operational expenses."""
        summary = db.get_token_summary()
        budget = self.get_budget_status()
        total_cost_inr = summary["total_cost_inr"]
        net_profit_after_ai = round(realized_pnl - total_cost_inr, 2)

        return {
            "total_tokens_used": summary["total_tokens"],
            "today_tokens_used": budget["today_tokens_used"],
            "today_prompt_tokens": budget["today_prompt_tokens"],
            "today_completion_tokens": budget["today_completion_tokens"],
            "max_daily_tokens": MAX_DAILY_TOKENS,
            "max_daily_input_tokens": MAX_DAILY_INPUT_TOKENS,
            "max_daily_output_tokens": MAX_DAILY_OUTPUT_TOKENS,
            "remaining_daily_tokens": budget["remaining_daily_tokens"],
            "remaining_input_tokens": budget["remaining_input_tokens"],
            "remaining_output_tokens": budget["remaining_output_tokens"],
            "daily_budget_used_pct": budget["daily_budget_used_pct"],
            "input_budget_used_pct": budget["input_budget_used_pct"],
            "output_budget_used_pct": budget["output_budget_used_pct"],
            "is_throttled": budget["is_throttled"],
            "total_calls": summary["total_calls"],
            "estimated_ai_cost_inr": total_cost_inr,
            "realized_trading_pnl_inr": round(realized_pnl, 2),
            "net_profit_after_ai_cost_inr": net_profit_after_ai,
            "breakdown_by_model": summary["breakdown_by_model"],
        }


token_tracker = TokenTracker()

