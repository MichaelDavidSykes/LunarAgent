import json
from typing import Any, Dict, List, Optional

import httpx

from .config import settings


def _trim_text(value: Any, max_len: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _strip_code_fences(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            value = "\n".join(lines[1:-1]).strip()
    return value


def _safe_parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return None

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def _normalize_text_list(value: Any, max_items: int = 4, max_len: int = 140) -> List[str]:
    if isinstance(value, str):
        candidates = [line.strip("-• \t") for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        candidates = [_trim_text(item, max_len=max_len) for item in value]
    else:
        candidates = []

    out: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = _trim_text(item, max_len=max_len).strip()
        normalized = text.lower()
        if not text or normalized in seen:
            continue
        seen.add(normalized)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _normalize_module_key(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.startswith("module-"):
        text = text[len("module-") :]
    text = text.replace(" module", "").strip()
    if not text:
        return None
    return f"module-{text}"


def _default_action_label(action_type: str, country_name: Optional[str], module_keys: List[str]) -> str:
    if action_type == "focus_country" and country_name:
        return f"Focus {country_name}"
    if action_type == "clear_country_focus":
        return "Clear location focus"
    if action_type == "apply_module_filter" and module_keys:
        module_name = module_keys[0].replace("module-", "").replace("-", " ").strip()
        module_name = module_name.title() if module_name else "Module"
        return f"Filter to {module_name}"
    if action_type == "clear_module_filters":
        return "Clear module filters"
    if action_type == "open_map":
        return "Open flat map"
    return "Run action"


def _normalize_action(action: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(action, dict):
        return None

    action_type = str(action.get("type") or "").strip().lower()
    if action_type not in {
        "focus_country",
        "clear_country_focus",
        "apply_module_filter",
        "clear_module_filters",
        "open_map",
    }:
        return None

    country_name = _trim_text(action.get("countryName"), 80) or None
    country_code_raw = str(action.get("countryCode") or "").strip().upper()
    country_code = country_code_raw if len(country_code_raw) == 2 and country_code_raw.isalpha() else None
    module_keys = []
    if isinstance(action.get("moduleKeys"), list):
        for item in action["moduleKeys"]:
            normalized = _normalize_module_key(item)
            if normalized and normalized not in module_keys:
                module_keys.append(normalized)

    if action_type == "focus_country" and not (country_name or country_code):
        return None
    if action_type == "apply_module_filter" and not module_keys:
        return None

    reason = _trim_text(action.get("reason"), 180) or None
    label = _trim_text(action.get("label"), 80) or _default_action_label(action_type, country_name, module_keys)

    payload: Dict[str, Any] = {
        "type": action_type,
        "label": label,
    }
    if reason:
        payload["reason"] = reason
    if country_name:
        payload["countryName"] = country_name
    if country_code:
        payload["countryCode"] = country_code
    if module_keys:
        payload["moduleKeys"] = module_keys
    return payload


def build_prompt_messages(
    session_id: Optional[str],
    allow_ui_actions: bool,
    conversation_history: List[Dict[str, str]],
    query_preview: str,
    summary: Dict[str, Any],
    context: Dict[str, Any],
    user_message: str,
) -> List[Dict[str, str]]:
    system_prompt = (
        "You are Lunar Explorer Agent inside LunarChain Explorer. "
        "Your primary job is to evaluate and explain the intelligence in the current Explorer scope. "
        "Use only the provided query summary, explicit evidence, and any report-reading tools available to you. "
        "Do not invent data, entities, report contents, or causal relationships. "
        "If evidence is only co-occurrence, say that clearly. "
        "Do not recommend filters, pivots, map views, or UI changes unless the user explicitly asks for them. "
        "Default to actions=[] and follow_ups=[]. "
        "Return strict JSON only."
    )

    payload = {
        "agent": {
            "name": "Lunar Explorer Agent",
            "runtime": "lunar-agent",
            "mode": "interactive-intelligence-analysis",
        },
        "sessionId": str(session_id or "").strip() or None,
        "allowUiActions": bool(allow_ui_actions),
        "allowedActions": [
            {
                "type": "focus_country",
                "when": "Only if the user explicitly asks you to focus the Explorer view on a country or location",
                "requiredFields": ["countryName or countryCode"],
            },
            {
                "type": "clear_country_focus",
                "when": "Only if the user explicitly asks you to clear the current location focus",
                "requiredFields": [],
            },
            {
                "type": "apply_module_filter",
                "when": "Only if the user explicitly asks you to filter Explorer to one or more intelligence modules",
                "requiredFields": ["moduleKeys"],
            },
            {
                "type": "clear_module_filters",
                "when": "Only if the user explicitly asks you to clear module filters",
                "requiredFields": [],
            },
            {
                "type": "open_map",
                "when": "Only if the user explicitly asks to open the flat map",
                "requiredFields": [],
            },
        ] if allow_ui_actions else [],
        "toolPolicy": [
            "If the user asks about a specific actor, country, IOC, malware family, source, campaign, or topic, call search_scope_reports first.",
            "If the question asks what the intelligence actually says, inspect scoped reports before answering.",
            "Use list_scope_reports for broad orientation, search_scope_reports for entity or phrase questions, and get_scope_report_detail before making a precise report-level claim.",
            "Prefer report text and explicit relationship evidence over high-level counters.",
        ],
        "responseShape": {
            "reply": "markdown string",
            "actions": [
                {
                    "type": "focus_country | clear_country_focus | apply_module_filter | clear_module_filters | open_map",
                    "label": "short button label",
                    "reason": "short explanation",
                    "countryName": "optional string",
                    "countryCode": "optional ISO-2 string",
                    "moduleKeys": ["optional module keys like module-maritime"],
                }
            ] if allow_ui_actions else [],
            "follow_ups": ["short suggested follow-up questions"],
        },
        "instructions": [
            "Answer the user's question directly and analytically.",
            "Ground claims in the provided summary, relationship evidence, and report-reading tool outputs only.",
            "Do not answer a substantive intelligence question until you have inspected at least one scoped report tool result in this turn.",
            (
                "If allowUiActions=false, return actions=[] and follow_ups=[] and do not recommend filtering, "
                "pivoting, opening the map, or changing the Explorer UI."
            ),
            "Default to actions=[] and follow_ups=[].",
            "Only include actions if the user explicitly asks you to change or inspect the Explorer UI and allowUiActions=true.",
            "Keep replies readable in a chat window.",
            "Do not wrap the JSON in code fences.",
        ],
        "conversationHistory": conversation_history,
        "currentUserMessage": _trim_text(user_message, 1600),
        "queryPreview": _trim_text(query_preview, 400),
        "queryContext": context,
        "querySummary": summary,
    }

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def normalize_response_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    reply = _trim_text(payload.get("reply"), 12000)
    actions = []
    for action in payload.get("actions") or []:
        normalized = _normalize_action(action)
        if normalized:
            actions.append(normalized)
        if len(actions) >= 3:
            break

    follow_ups = _normalize_text_list(payload.get("follow_ups") or payload.get("followUps"), max_items=4, max_len=120)

    return {
        "reply": reply or "I couldn't produce a structured answer for this query yet.",
        "actions": actions,
        "followUps": follow_ups,
    }


def normalize_model_response(raw_text: str) -> Dict[str, Any]:
    parsed = _safe_parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return {
            "reply": _trim_text(raw_text, 12000) or "I couldn't produce a structured answer for this query yet.",
            "actions": [],
            "followUps": [],
        }
    return normalize_response_payload(parsed)


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    parts.append(value)
                continue
            if not isinstance(item, dict):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                parts.append(text_value.strip())
            elif isinstance(text_value, dict):
                nested = text_value.get("value")
                if isinstance(nested, str) and nested.strip():
                    parts.append(nested.strip())
            nested_content = item.get("content")
            if isinstance(nested_content, str) and nested_content.strip():
                parts.append(nested_content.strip())
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text_value = content.get("text")
        if isinstance(text_value, str) and text_value.strip():
            return text_value.strip()
        nested = content.get("value")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _extract_text_from_chat_response(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if isinstance(message, dict):
            content_text = _extract_text_from_content(message.get("content"))
            if content_text:
                return content_text
            refusal = message.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                return refusal.strip()
        choice_text = first_choice.get("text") if isinstance(first_choice, dict) else None
        if isinstance(choice_text, str) and choice_text.strip():
            return choice_text.strip()
    return ""


def _tool_specs() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_scope_reports",
                "description": "Read the most relevant reports in the current Explorer scope before summarizing what the intelligence says.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12}
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_scope_reports",
                "description": "Search scoped reports for an actor, location, IOC, organization, phrase, or topic and return matching report snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_scope_report_detail",
                "description": "Read fuller detail for one scoped report before making a report-level claim.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "report_id": {"type": "string"}
                    },
                    "required": ["report_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _backend_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(settings.backend_shared_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _call_backend_tool(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    base_url = str(settings.backend_base_url or "").strip()
    if not base_url:
        raise RuntimeError("LUNAR_AGENT_BACKEND_BASE_URL is not configured")

    target = f"{base_url.rstrip('/')}{path}"
    timeout = max(10, int(settings.backend_http_timeout))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(target, headers=_backend_headers(), json=payload)

    if response.status_code >= 400:
        raise RuntimeError(f"Backend tool call failed with HTTP {response.status_code}: {response.text[:400]}")
    return response.json() if response.content else {}


async def _execute_tool_call(name: str, arguments: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
    scoped_session_id = str(session_id or "").strip()
    if not scoped_session_id:
        return {"error": "No Explorer agent session is available for tool use."}

    if name == "list_scope_reports":
        limit = int(arguments.get("limit") or 6)
        return await _call_backend_tool(
            "/api/v1/graph/ai-agent/tools/list-reports",
            {"session_id": scoped_session_id, "limit": max(1, min(limit, 12))},
        )

    if name == "search_scope_reports":
        query = _trim_text(arguments.get("query"), 240)
        limit = int(arguments.get("limit") or 6)
        return await _call_backend_tool(
            "/api/v1/graph/ai-agent/tools/search-reports",
            {"session_id": scoped_session_id, "query": query, "limit": max(1, min(limit, 10))},
        )

    if name == "get_scope_report_detail":
        report_id = _trim_text(arguments.get("report_id"), 240)
        return await _call_backend_tool(
            "/api/v1/graph/ai-agent/tools/get-report",
            {"session_id": scoped_session_id, "report_id": report_id},
        )

    return {"error": f"Unknown tool: {name}"}


async def _chat_completion_request(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    timeout = max(10, int(settings.http_timeout))
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 900,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response: Optional[httpx.Response] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        if response.status_code == 400 and "max_tokens" in (response.text or ""):
            retry_payload = dict(payload)
            retry_payload.pop("max_tokens", None)
            retry_payload["max_completion_tokens"] = 900
            response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=retry_payload)
        if response.status_code == 400 and "temperature" in (response.text or ""):
            retry_payload = dict(payload)
            retry_payload.pop("temperature", None)
            if "max_completion_tokens" not in retry_payload and "max_tokens" not in retry_payload:
                retry_payload["max_completion_tokens"] = 900
            response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=retry_payload)

    if response is None:
        raise RuntimeError("No response received from OpenAI")
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI returned HTTP {response.status_code}: {response.text[:400]}")

    return response.json()


async def run_openai_analysis(messages: List[Dict[str, Any]]) -> str:
    data = await _chat_completion_request(messages)
    parsed_text = _extract_text_from_chat_response(data)
    if parsed_text:
        return parsed_text
    return "I couldn't produce a structured answer for this query yet."


async def run_tool_aware_analysis(messages: List[Dict[str, Any]], session_id: Optional[str]) -> str:
    if not str(settings.backend_base_url or "").strip() or not str(session_id or "").strip():
        return await run_openai_analysis(messages)

    working_messages: List[Dict[str, Any]] = list(messages)
    tools = _tool_specs()
    saw_tool_result = False
    for _ in range(4):
        try:
            data = await _chat_completion_request(working_messages, tools=tools)
        except Exception:
            return await run_openai_analysis(messages)
        choices = data.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else {}
        if not isinstance(message, dict):
            break

        tool_calls = message.get("tool_calls")
        content_text = _extract_text_from_content(message.get("content"))

        if isinstance(tool_calls, list) and tool_calls:
            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": content_text or "",
                "tool_calls": tool_calls,
            }
            working_messages.append(assistant_message)

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                tool_id = str(tool_call.get("id") or "").strip()
                function_data = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                tool_name = str(function_data.get("name") or "").strip()
                raw_arguments = function_data.get("arguments")
                try:
                    parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else {}
                except Exception:
                    parsed_arguments = {}

                try:
                    result = await _execute_tool_call(tool_name, parsed_arguments if isinstance(parsed_arguments, dict) else {}, session_id)
                except Exception as exc:
                    result = {"error": str(exc)}

                saw_tool_result = True
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            continue

        if content_text:
            if not saw_tool_result:
                working_messages.append({
                    "role": "system",
                    "content": (
                        "Before answering, inspect scoped reports with one of the available tools. "
                        "Use search_scope_reports for specific topics/entities or list_scope_reports for broad orientation."
                    ),
                })
                continue
            return content_text

    return await run_openai_analysis(messages)


async def respond(
    session_id: Optional[str],
    allow_ui_actions: bool,
    conversation_history: List[Dict[str, str]],
    query_preview: str,
    summary: Dict[str, Any],
    context: Dict[str, Any],
    user_message: str,
) -> Dict[str, Any]:
    messages = build_prompt_messages(
        session_id=session_id,
        allow_ui_actions=allow_ui_actions,
        conversation_history=conversation_history,
        query_preview=query_preview,
        summary=summary,
        context=context,
        user_message=user_message,
    )
    raw_answer = await run_tool_aware_analysis(messages, session_id=session_id)
    normalized = normalize_model_response(raw_answer)
    normalized["model"] = settings.model
    return normalized
