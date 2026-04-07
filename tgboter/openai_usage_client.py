from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tgboter.config import Config


@dataclass(slots=True)
class OpenAIUsageSummary:
    """Aggregated usage and cost information for Telegram display."""

    local_date_label: str
    project_scope: str
    today_input_tokens: int
    today_output_tokens: int
    today_total_tokens: int
    last_30d_total_tokens: int
    last_30d_input_tokens: int
    last_30d_output_tokens: int
    last_30d_cost_value: Decimal | None
    last_30d_cost_currency: str | None


class OpenAIUsageClient:
    """Fetch organization usage and costs from OpenAI administration APIs."""

    _BASE_URL = "https://api.openai.com/v1/organization"

    def __init__(self, config: Config) -> None:
        self.config = config

    def is_configured(self) -> bool:
        """Whether an admin API key is available for usage queries."""
        return bool(self.config.openai_admin_api_key.strip())

    async def get_usage_summary(self) -> OpenAIUsageSummary:
        """Return today's token usage and the last 30 days of usage and cost."""
        if not self.is_configured():
            raise RuntimeError("OpenAI admin API key is not configured")

        now = datetime.now().astimezone()
        local_midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
        start_today = int(local_midnight.timestamp())
        start_30d = int((local_midnight - timedelta(days=29)).timestamp())
        end_time = int(now.timestamp()) + 1

        usage_payload, costs_payload = await asyncio.gather(
            self._get_json(
                "/usage/completions",
                start_time=start_30d,
                end_time=end_time,
                bucket_width="1d",
                limit=30,
            ),
            self._get_json(
                "/costs",
                start_time=start_30d,
                end_time=end_time,
                bucket_width="1d",
                limit=30,
            ),
        )

        usage_buckets = usage_payload.get("data", [])
        cost_buckets = costs_payload.get("data", [])

        today_input = 0
        today_output = 0
        total_input = 0
        total_output = 0
        cost_value = Decimal("0")
        cost_currency: str | None = None

        for bucket in usage_buckets:
            bucket_start = int(bucket.get("start_time", 0))
            for result in bucket.get("results", []):
                input_tokens = int(result.get("input_tokens", 0) or 0)
                output_tokens = int(result.get("output_tokens", 0) or 0)
                total_input += input_tokens
                total_output += output_tokens
                if bucket_start >= start_today:
                    today_input += input_tokens
                    today_output += output_tokens

        for bucket in cost_buckets:
            for result in bucket.get("results", []):
                amount = result.get("amount") or {}
                value = amount.get("value")
                if value is None:
                    continue
                cost_value += Decimal(str(value))
                if cost_currency is None:
                    currency = amount.get("currency")
                    if isinstance(currency, str) and currency.strip():
                        cost_currency = currency.upper()

        project_scope = self.config.openai_project_id.strip() or "organization"
        return OpenAIUsageSummary(
            local_date_label=now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            project_scope=project_scope,
            today_input_tokens=today_input,
            today_output_tokens=today_output,
            today_total_tokens=today_input + today_output,
            last_30d_total_tokens=total_input + total_output,
            last_30d_input_tokens=total_input,
            last_30d_output_tokens=total_output,
            last_30d_cost_value=cost_value,
            last_30d_cost_currency=cost_currency,
        )

    async def _get_json(self, path: str, **params: Any) -> dict[str, Any]:
        """Perform a GET request against the OpenAI administration APIs."""
        return await asyncio.to_thread(self._get_json_sync, path, params)

    def _get_json_sync(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query_params: list[tuple[str, str]] = []
        for key, value in params.items():
            if value in (None, "", []):
                continue
            if isinstance(value, (list, tuple)):
                query_params.extend((key, str(item)) for item in value)
                continue
            query_params.append((key, str(value)))

        project_id = self.config.openai_project_id.strip()
        if project_id:
            query_params.append(("project_ids", project_id))

        url = f"{self._BASE_URL}{path}?{urlencode(query_params, doseq=True)}"
        headers = {
            "Authorization": f"Bearer {self.config.openai_admin_api_key}",
            "Content-Type": "application/json",
        }
        if self.config.openai_organization_id.strip():
            headers["OpenAI-Organization"] = self.config.openai_organization_id.strip()

        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)
