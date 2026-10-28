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
MAX_DAILY_TOKENS = 100_000  # Strict daily limit requested by user

MODEL_PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-3.8-flash": {"input": 0.15, "output": 0.60},
    "gemini-4-argon": {"input": 0.20, "output": 0.80},
}


class TokenTracker:
    """Tracks token consumption, enforces strict 100K/day budget, and calculates AI costs in INR."""

    def can_consume_tokens(self, estimated_tokens: int = 500, is_emergency: bool = False) -> bool:
        """Enforce strict 100K daily token cap to prevent credit overrun."""
        today_used = db.get_today_tokens()
        if is_emergency:
            # Allow critical emergency decisions up to 110K hard ceiling
            return (today_used + estimated_tokens) <= (MAX_DAILY_TOKENS * 1.1)
        return (today_used + estimated_tokens) <= MAX_DAILY_TOKENS

    def get_budget_status(self) -> Dict[str, Any]:
        """Get daily 100K quota consumption status."""
        today_used = db.get_today_tokens()
        remaining = max(0, MAX_DAILY_TOKENS - today_used)
        used_pct = round((today_used / MAX_DAILY_TOKENS) * 100, 2)
        return {
            "max_daily_tokens": MAX_DAILY_TOKENS,
            "today_tokens_used": today_used,
            "remaining_daily_tokens": remaining,
            "daily_budget_used_pct": min(used_pct, 100.0),
            "is_throttled": today_used >= MAX_DAILY_TOKENS,
        }

    def calculate_cost_inr(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Compute estimated cost in Indian Rupees."""
        rates = MODEL_PRICING.get(model.lower(), {"input": 0.15, "output": 0.60})
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
            "max_daily_tokens": MAX_DAILY_TOKENS,
            "remaining_daily_tokens": budget["remaining_daily_tokens"],
            "daily_budget_used_pct": budget["daily_budget_used_pct"],
            "is_throttled": budget["is_throttled"],
            "total_calls": summary["total_calls"],
            "estimated_ai_cost_inr": total_cost_inr,
            "realized_trading_pnl_inr": round(realized_pnl, 2),
            "net_profit_after_ai_cost_inr": net_profit_after_ai,
            "breakdown_by_model": summary["breakdown_by_model"],
        }


token_tracker = TokenTracker()

