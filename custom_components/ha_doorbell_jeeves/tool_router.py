"""Tool router – manages a separate text model for tool calling in dual-model mode.

When using native audio models (like gemini-2.5-flash-native-audio-dialog) that
don't support function calling, this module provides a separate text-based model
that monitors the conversation transcript and executes tool calls as needed.

Architecture:
  Voice model: handles real-time audio I/O (speech-to-speech)
  Tool model: receives conversation transcript, decides when to call tools
  
Flow:
  1. Voice model generates speech + transcript (no tools declared)
  2. Tool router receives transcript updates
  3. After each assistant turn, tool router sends context to tool model
  4. If tool model returns function calls, they're executed
  5. Results are injected back into the voice session as text context
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Debounce: wait this long after last transcript before triggering tool check
TOOL_CHECK_DEBOUNCE_S = 1.5


class ToolRouter:
    """Routes tool calls through a separate text model.
    
    The tool router accumulates conversation transcript and periodically
    asks a text model whether any tools should be called based on the
    current conversation state.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt: str,
        tools: list[Any],
        on_tool_call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        on_inject_context: Callable[[str], Awaitable[None]],
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._system_prompt = system_prompt
        self._tools = tools
        self._on_tool_call = on_tool_call
        self._on_inject_context = on_inject_context

        self._conversation: list[dict[str, str]] = []
        self._pending_check: asyncio.Task[None] | None = None
        self._active = False
        self._processing = False
        self._last_processed_index = 0

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self) -> None:
        """Activate the tool router."""
        self._active = True
        self._conversation.clear()
        self._last_processed_index = 0
        _LOGGER.info("Tool router started (model=%s)", self._model)

    def stop(self) -> None:
        """Deactivate and clean up."""
        self._active = False
        if self._pending_check and not self._pending_check.done():
            self._pending_check.cancel()
        self._pending_check = None
        self._conversation.clear()

    def add_transcript(self, role: str, text: str) -> None:
        """Add a transcript entry and schedule a tool check."""
        if not self._active or not text.strip():
            return

        self._conversation.append({"role": role, "content": text})

        # Only trigger tool check after assistant speaks (they might be requesting action)
        if role == "assistant":
            self._schedule_tool_check()

    def _schedule_tool_check(self) -> None:
        """Debounced tool check – waits for speech to settle before checking."""
        if self._pending_check and not self._pending_check.done():
            self._pending_check.cancel()
        self._pending_check = asyncio.create_task(self._debounced_check())

    async def _debounced_check(self) -> None:
        """Wait for debounce period then run tool check."""
        try:
            await asyncio.sleep(TOOL_CHECK_DEBOUNCE_S)
            if self._active and not self._processing:
                await self._run_tool_check()
        except asyncio.CancelledError:
            pass

    async def _run_tool_check(self) -> None:
        """Send conversation to tool model and execute any requested tools."""
        if not self._conversation or self._processing:
            return

        # Only process new turns since last check
        new_turns = self._conversation[self._last_processed_index:]
        if not new_turns:
            return

        self._processing = True
        self._last_processed_index = len(self._conversation)

        try:
            if self._provider == "gemini":
                tool_calls = await self._check_gemini(new_turns)
            else:
                tool_calls = await self._check_openai(new_turns)

            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("arguments", {})
                _LOGGER.info("Tool router: calling %s(%s)", name, json.dumps(args)[:100])

                try:
                    result = await self._on_tool_call(name, args)
                    # Inject result context back into voice session
                    context_msg = self._format_tool_result(name, args, result)
                    await self._on_inject_context(context_msg)
                    # Also add to our conversation for future context
                    self._conversation.append({
                        "role": "system",
                        "content": f"[Tool result: {name}] {json.dumps(result)[:500]}",
                    })
                except Exception:
                    _LOGGER.exception("Tool router: failed to execute %s", name)

        except Exception:
            _LOGGER.exception("Tool router: check failed")
        finally:
            self._processing = False

    def _format_tool_result(self, name: str, args: dict, result: dict) -> str:
        """Format a tool result as text to inject into the voice session."""
        if result.get("error"):
            return f"[System: Action '{name}' was blocked or failed: {result['error']}]"

        # Format based on tool type
        if name == "view_camera":
            # Image is handled separately, just give text summary
            summary = result.get("summary", result.get("description", "Camera snapshot taken"))
            return f"[System: Camera view result - {summary}]"
        elif name == "get_calendar_events":
            events = result.get("events", [])
            if events:
                event_text = "; ".join(
                    f"{e.get('summary', 'Event')} ({e.get('start', '?')})"
                    for e in events[:5]
                )
                return f"[System: Upcoming events - {event_text}]"
            return "[System: No upcoming calendar events found.]"
        elif name == "get_entity_history":
            return f"[System: History result - {json.dumps(result)[:300]}]"
        elif name == "search_events":
            return f"[System: Event search result - {json.dumps(result)[:300]}]"
        elif name.startswith("notify_"):
            return f"[System: Notification sent successfully to {args.get('target', 'admin')}]"
        else:
            # Generic action result
            success = result.get("success", False)
            msg = result.get("message", "completed" if success else "failed")
            return f"[System: Action '{name}' {'completed' if success else 'failed'}: {msg}]"

    async def _check_gemini(self, new_turns: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Use Gemini text model to check if tools should be called."""
        import asyncio  # noqa: PLC0415

        from google import genai  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        # genai.Client() does blocking I/O — run in executor
        loop = asyncio.get_event_loop()
        client = await loop.run_in_executor(
            None, lambda: genai.Client(api_key=self._api_key)
        )

        # Build messages
        messages = self._build_tool_check_messages(new_turns)

        try:
            response = await client.aio.models.generate_content(
                model=self._model,
                contents=[types.Content(role="user", parts=[types.Part(text=messages)])],
                config=types.GenerateContentConfig(
                    system_instruction=self._build_tool_system_prompt(),
                    tools=self._tools if self._tools else None,
                    temperature=0.1,
                ),
            )
        except Exception:
            _LOGGER.exception("Gemini tool model request failed")
            return []

        # Extract function calls from response
        tool_calls = []
        if response and response.candidates:
            for candidate in response.candidates:
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        fc = getattr(part, "function_call", None)
                        if fc:
                            tool_calls.append({
                                "name": fc.name,
                                "arguments": dict(fc.args) if fc.args else {},
                            })
        return tool_calls

    async def _check_openai(self, new_turns: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Use OpenAI text model to check if tools should be called."""
        import openai  # noqa: PLC0415

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        client = openai.AsyncOpenAI(**client_kwargs)

        messages = [
            {"role": "system", "content": self._build_tool_system_prompt()},
            {"role": "user", "content": self._build_tool_check_messages(new_turns)},
        ]

        try:
            chat_tools = self._normalize_openai_chat_tools(self._tools)
            response = await client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=chat_tools if chat_tools else None,
                tool_choice="auto",
                temperature=0.1,
            )
        except Exception:
            _LOGGER.exception("OpenAI tool model request failed")
            return []

        tool_calls = []
        if response.choices and response.choices[0].message.tool_calls:
            for tc in response.choices[0].message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({"name": tc.function.name, "arguments": args})
        return tool_calls

    def _build_tool_system_prompt(self) -> str:
        """Build the system prompt for the tool-checking model."""
        return (
            f"{self._system_prompt}\n\n"
            "--- TOOL ROUTING INSTRUCTIONS ---\n"
            "You are the tool-calling component of an AI doorbell concierge. "
            "You receive the recent conversation transcript between the voice AI and a visitor. "
            "Your job is to decide if any tools should be called based on what was discussed.\n\n"
            "Rules:\n"
            "- Only call tools when the conversation clearly indicates an action should be taken.\n"
            "- Do NOT call tools for things already handled or mentioned as completed.\n"
            "- If no tools are needed, respond with a brief 'No action needed' text.\n"
            "- Be conservative: only act on clear, explicit requests or situations.\n"
            "- If the AI said it would do something (like check a camera, turn on a light), "
            "call the appropriate tool.\n"
        )

    def _build_tool_check_messages(self, new_turns: list[dict[str, str]]) -> str:
        """Format conversation turns into a message for the tool model."""
        # Include some history for context
        history_context = self._conversation[max(0, self._last_processed_index - 10):self._last_processed_index]
        
        parts = []
        if history_context:
            parts.append("=== CONVERSATION CONTEXT ===")
            for turn in history_context[-5:]:
                parts.append(f"{turn['role'].upper()}: {turn['content']}")

        parts.append("\n=== NEW TURNS (decide if tools are needed) ===")
        for turn in new_turns:
            parts.append(f"{turn['role'].upper()}: {turn['content']}")

        parts.append(
            "\n--- Based on the above conversation, should any tools be called? "
            "If yes, call them. If no, respond with 'No action needed.' ---"
        )
        return "\n".join(parts)

    def _normalize_openai_chat_tools(self, tools: list[Any]) -> list[Any]:
        """Normalize tool schema for OpenAI chat completions."""
        normalized: list[Any] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if "function" in tool:
                normalized.append(tool)
                continue
            if tool.get("type") == "function" and "name" in tool:
                normalized.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.get("name", ""),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                        },
                    }
                )
        return normalized
