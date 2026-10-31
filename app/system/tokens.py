"""AI token usage and operating-cost accounting.

Costs are attributed to the account that caused the call.  The dashboard can
therefore show gross trading P&L, transaction fees, AI cost, and the true net
result after AI.
"""

from typing import Any, Dict, Optional

from app.config import settings
from app.storage.db import db

USD_TO_INR = 84.50
MAX_DAILY_INPUT_TOKENS = 10_000_000
MAX_DAILY_OUTPUT_TOKENS = 500_000
MAX_DAILY_TOKENS = MAX_DAILY_INPUT_TOKENS + MAX_DAILY_OUTPUT_TOKENS

MODEL_PRICING = {
    settings.DEEPSEEK_MODEL.lower(): {"input": 0.14, "output": 0.28},
    "deepseek-ai/deepseek-v4.1-flash-maas": {"input": 0.14, "output": 0.28},
    # Claude Opus is invoked through the local Antigravity CLI on the Pro plan,
    # so there is no per-token cloud charge. Keep the entry explicit.
    settings.OPUS_MODEL.lower(): {"input": 0.0, "output": 0.0},
    "claude-opus-4-6-thinking": {"input": 0.0, "output": 0.0},
    "claude-sonnet-4-6": {"input": 0.0, "output": 0.0},
}


class TokenTracker:
    def can_consume_tokens(
        self,
        estimated_prompt_tokens: int = 50_000,
        estimated_completion_tokens: Optional[int] = None,
        is_emergency: bool = False,
    ) -> bool:
        prompt_est = max(0, int(estimated_prompt_tokens))
        comp_est = 500 if estimated_completion_tokens is None else max(0, int(estimated_completion_tokens))
        breakdown = db.get_today_token_breakdown()
        prompt_cap = MAX_DAILY_INPUT_TOKENS * (1.15 if is_emergency else 1.0)
        comp_cap = MAX_DAILY_OUTPUT_TOKENS * (1.15 if is_emergency else 1.0)
        return (
            breakdown["prompt_tokens"] + prompt_est <= prompt_cap
            and breakdown["completion_tokens"] + comp_est <= comp_cap
        )

    def get_budget_status(self) -> Dict[str, Any]:
        breakdown = db.get_today_token_breakdown()
        today_prompt = breakdown["prompt_tokens"]
        today_comp = breakdown["completion_tokens"]
        today_total = breakdown["total_tokens"]
        is_throttled = (
            today_prompt >= MAX_DAILY_INPUT_TOKENS
            or today_comp >= MAX_DAILY_OUTPUT_TOKENS
        )
        return {
            "max_daily_tokens": MAX_DAILY_TOKENS,
            "max_daily_input_tokens": MAX_DAILY_INPUT_TOKENS,
            "max_daily_output_tokens": MAX_DAILY_OUTPUT_TOKENS,
            "today_tokens_used": today_total,
            "today_prompt_tokens": today_prompt,
            "today_completion_tokens": today_comp,
            "remaining_daily_tokens": max(0, MAX_DAILY_TOKENS - today_total),
            "remaining_input_tokens": max(0, MAX_DAILY_INPUT_TOKENS - today_prompt),
            "remaining_output_tokens": max(0, MAX_DAILY_OUTPUT_TOKENS - today_comp),
            "daily_budget_used_pct": min(round((today_total / MAX_DAILY_TOKENS) * 100, 2), 100.0),
            "input_budget_used_pct": min(round((today_prompt / MAX_DAILY_INPUT_TOKENS) * 100, 2), 100.0),
            "output_budget_used_pct": min(round((today_comp / MAX_DAILY_OUTPUT_TOKENS) * 100, 2), 100.0),
            "is_throttled": is_throttled,
        }

    def calculate_cost_inr(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates = MODEL_PRICING.get(
            (model or "").lower(), {"input": 0.14, "output": 0.28}
        )
        cost_usd = (
            (max(0, prompt_tokens) / 1_000_000) * rates["input"]
            + (max(0, completion_tokens) / 1_000_000) * rates["output"]
        )
        return round(cost_usd * USD_TO_INR, 4)

    def log_usage(
        self,
        module: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        account_id: str = "shared",
    ) -> float:
        cost = self.calculate_cost_inr(model, prompt_tokens, completion_tokens)
        db.record_token_usage(
            module=module,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_inr=cost,
            account_id=account_id,
        )
        return cost

    def get_summary(
        self,
        gross_trading_pnl: float = 0.0,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = db.get_token_summary(account_id=account_id)
        budget = self.get_budget_status()
        ai_cost = float(summary["total_cost_inr"])
        return {
            "account_id": account_id or "all_accounts",
            **budget,
            "total_tokens_used": summary["total_tokens"],
            "total_calls": summary["total_calls"],
            "estimated_ai_cost_inr": ai_cost,
            "gross_trading_pnl_inr": round(gross_trading_pnl, 2),
            "net_profit_after_ai_cost_inr": round(gross_trading_pnl - ai_cost, 2),
            "breakdown_by_model": summary["breakdown_by_model"],
        }


token_tracker = TokenTracker()
