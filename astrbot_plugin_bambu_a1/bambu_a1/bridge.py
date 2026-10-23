"""LLM 桥：超时、预算、降级、供应商选择。

关键约束（ARCHITECTURE.md 6.5）：
  - 任何失败都返回 None，让上层退回模板直推 —— 告警永远不依赖模型活着
  - 状态事实走单轮 user 内容（<facts> 段），不塞 system_prompt，避免破坏提示词缓存
  - 每小时调用上限默认 12 次，超出直接返回 None
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any, Callable

from . import prompts
from .models import (
    EV_HMS_ERROR,
    EV_PERIODIC_DIGEST,
    EV_PRINT_FAILED,
    EV_PRINT_FINISHED,
    EV_TASK_SUMMARY,
    DeviceEvent,
    Snapshot,
)


class LlmBridge:
    def __init__(
        self,
        context: Any,
        config: Callable[[str, Any], Any],
        hooks: Any = None,
        logger: Any = None,
        history_provider: Callable[[int], list[dict[str, Any]]] | None = None,
    ):
        self.context = context
        self._config = config
        self._hooks = hooks
        self.log = logger
        self._history = history_provider or (lambda _n: [])
        self._calls: deque[float] = deque()
        self._last_error = ""
        self._last_skip_reason = ""

    # ── 预算 ────────────────────────────────────────────────
    def _prune_budget(self) -> None:
        cutoff = time.time() - 3600
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()

    def budget_ok(self) -> tuple[bool, str]:
        self._prune_budget()
        limit = int(self._config("llm_budget_per_hour", 12) or 0)
        if limit <= 0:
            return True, ""  # 0 表示不限
        if len(self._calls) >= limit:
            return False, f"已达每小时上限 {limit} 次"
        return True, ""

    def budget_status(self) -> dict[str, Any]:
        self._prune_budget()
        return {
            "used_last_hour": len(self._calls),
            "limit": int(self._config("llm_budget_per_hour", 12) or 0),
            "last_error": self._last_error,
        }

    def _mode(self) -> str:
        return str(self._config("llm_mode", "summary_only") or "summary_only").lower()

    # ── 底层调用 ────────────────────────────────────────────
    async def complete(
        self,
        umo: str,
        prompt: str,
        system_prompt: str,
        *,
        kind: str = "generic",
    ) -> str | None:
        if self._mode() == "off":
            self._last_skip_reason = "llm_mode=off"
            return None
        ok, reason = self.budget_ok()
        if not ok:
            self._last_skip_reason = reason
            if self.log:
                with contextlib.suppress(Exception):
                    self.log.info("跳过 LLM(%s): %s", kind, reason)
            return None

        provider_id = str(self._config("llm_provider_id", "") or "")
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"获取供应商失败: {type(exc).__name__}"
                return None
        if not provider_id:
            self._last_error = "没有可用的 LLM 供应商"
            return None

        timeout = float(self._config("llm_timeout", 20) or 20)
        self._calls.append(time.time())
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._last_error = f"超时 {timeout:.0f}s"
            if self.log:
                with contextlib.suppress(Exception):
                    self.log.info("LLM 超时(%s)，回退模板", kind)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self.log:
                with contextlib.suppress(Exception):
                    self.log.warning("LLM 调用失败(%s): %s", kind, self._last_error)
            return None

        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            self._last_error = "模型返回空内容"
            return None
        self._last_error = ""

        if self._hooks:
            replaced = await self._hooks.emit_first("on_llm_draft", prompt, text)
            if isinstance(replaced, str) and replaced.strip():
                text = replaced.strip()
        return text

    # ── 事件补写（供 coalescer 调用）────────────────────────
    async def draft_for_event(self, event: DeviceEvent, snapshot: Snapshot | None = None,
                              umo: str = "") -> str | None:
        if event.type == EV_PRINT_FINISHED:
            task = {
                "task_name": event.data.get("task_name"),
                "elapsed_min": event.data.get("elapsed_min"),
                "total_layers": event.data.get("total_layers"),
            }
            return await self.complete(
                umo,
                prompts.summary_prompt(task, self._history(5)),
                prompts.SUMMARY_SYSTEM,
                kind="summary",
            )

        if event.type in (EV_PRINT_FAILED, EV_HMS_ERROR):
            return await self.complete(
                umo,
                prompts.diagnosis_prompt(event.data, self._history(8)),
                prompts.DIAGNOSIS_SYSTEM,
                kind="diagnosis",
            )

        if event.type in (EV_TASK_SUMMARY, EV_PERIODIC_DIGEST):
            if event.data.get("text"):
                return event.data["text"]
            return await self.complete(
                umo, prompts.digest_prompt(event.data), prompts.DIGEST_SYSTEM, kind="digest"
            )

        return None

    # ── 问答 ────────────────────────────────────────────────
    async def answer_status(self, umo: str, snapshot: dict[str, Any], question: str = "") -> str | None:
        return await self.complete(
            umo,
            prompts.status_answer_prompt(snapshot, question),
            prompts.STATUS_ANSWER_SYSTEM,
            kind="status_answer",
        )

    async def parse_rule(self, umo: str, text: str, known_types: list[str]) -> str | None:
        return await self.complete(
            umo, prompts.rule_parse_prompt(text, known_types), prompts.RULE_PARSE_SYSTEM, kind="rule_parse"
        )
