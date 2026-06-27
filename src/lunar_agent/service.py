import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)


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


def _extract_wrapped_reply_payload(value: Any) -> Optional[Dict[str, Any]]:
    text = _trim_text(value, 12000)
    if not text:
        return None

    parsed = _safe_parse_json_object(text)
    if not isinstance(parsed, dict):
        return None

    return parsed if any(key in parsed for key in ("reply", "answer", "message", "content")) else None


def _normalize_reply_text(value: Any, max_len: int = 12000) -> str:
    if isinstance(value, dict):
        value = value.get("reply") or value.get("answer") or value.get("message") or value.get("content")

    text = _trim_text(value, max_len)
    for _ in range(3):
        wrapped = _extract_wrapped_reply_payload(text)
        if not wrapped:
            break
        nested = wrapped.get("reply") or wrapped.get("answer") or wrapped.get("message") or wrapped.get("content")
        if not isinstance(nested, str) or not nested.strip():
            break
        next_text = _trim_text(nested, max_len)
        if next_text == text:
            break
        text = next_text
    return text


def _extract_responses_text(data: Dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: List[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                elif isinstance(text, dict):
                    value = text.get("value")
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
    return "\n".join(parts).strip()


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


def _coerce_finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _coerce_bounded_float(value: Any, minimum: float, maximum: float) -> Optional[float]:
    parsed = _coerce_finite_float(value)
    if parsed is None or parsed < minimum or parsed > maximum:
        return None
    return parsed


def _coerce_bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _coerce_finite_float(value)
    if parsed is None:
        parsed = float(default)
    return max(minimum, min(int(parsed), maximum))


def _normalize_area_risk_coordinates(value: Any) -> List[Dict[str, float]]:
    if not isinstance(value, list):
        return []

    coordinates: List[Dict[str, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        lat = _coerce_bounded_float(item.get("lat"), -90, 90)
        lon = _coerce_bounded_float(item.get("lon") if item.get("lon") is not None else item.get("lng"), -180, 180)
        if lat is None or lon is None:
            continue
        coordinates.append({"lat": lat, "lon": lon})
    return coordinates


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
    wrapped_reply_payload = _extract_wrapped_reply_payload(payload.get("reply"))
    reply = _normalize_reply_text(payload.get("reply"), 12000)
    actions = []
    for action in payload.get("actions") or (wrapped_reply_payload or {}).get("actions") or []:
        normalized = _normalize_action(action)
        if normalized:
            actions.append(normalized)
        if len(actions) >= 3:
            break

    follow_ups = _normalize_text_list(
        payload.get("follow_ups")
        or payload.get("followUps")
        or (wrapped_reply_payload or {}).get("follow_ups")
        or (wrapped_reply_payload or {}).get("followUps"),
        max_items=4,
        max_len=120,
    )

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


def _uses_completion_token_limit(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def _uses_reasoning_effort(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized.startswith("gpt-5")


def _bounded_area_risk_max_zones(max_zones: int) -> int:
    configured = max(1, min(int(settings.area_risk_max_zones_per_request or 6), 20))
    requested = max(1, min(int(max_zones or configured), 20))
    return min(requested, configured)


def _bounded_area_risk_evidence(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_items = max(1, min(int(settings.area_risk_max_evidence_items or 12), 40))
    bounded: List[Dict[str, Any]] = []
    for item in evidence[:max_items]:
        if not isinstance(item, dict):
            continue
        bounded.append({
            "title": _trim_text(item.get("title"), 180),
            "url": _trim_text(item.get("url"), 400),
            "source": _trim_text(item.get("source") or item.get("sourceName"), 120),
            "published_at": _trim_text(item.get("published_at") or item.get("publishedAt") or item.get("date"), 80),
            "snippet": _trim_text(item.get("snippet") or item.get("description") or item.get("summary"), 420),
        })
    return bounded


AREA_RISK_EVIDENCE_TERMS = {
    "attack",
    "attacks",
    "carjacking",
    "crime",
    "criminal",
    "disruption",
    "extortion",
    "hijacking",
    "kidnapping",
    "looting",
    "murder",
    "plundering",
    "police",
    "protest",
    "protests",
    "robbery",
    "shooting",
    "unrest",
    "violence",
    "violent",
}

AREA_RISK_LABEL_STOPWORDS = {
    "area",
    "areas",
    "article",
    "city",
    "crime",
    "criminal",
    "hotspot",
    "hotspots",
    "logistics",
    "police",
    "public",
    "report",
    "reports",
    "risk",
    "risks",
    "road",
    "route",
    "routes",
    "safety",
    "security",
    "smoke",
    "source",
    "sources",
    "transport",
}


def _area_risk_context_labels(aoi: Dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    label_context = aoi.get("labelContext") if isinstance(aoi.get("labelContext"), dict) else {}
    for value in [
        label_context.get("place"),
        label_context.get("country"),
        label_context.get("display"),
        *(aoi.get("countryHints") if isinstance(aoi.get("countryHints"), list) else []),
    ]:
        text = str(value or "").strip()
        if not text:
            continue
        labels.add(text.casefold())
        for part in text.split(","):
            part = part.strip()
            if part:
                labels.add(part.casefold())
    return labels


def _area_risk_text_has_term(text: str) -> bool:
    normalized = str(text or "").casefold()
    return any(term in normalized for term in AREA_RISK_EVIDENCE_TERMS)


def _area_risk_terms_in_text(text: str, limit: int = 5) -> List[str]:
    normalized = str(text or "").casefold()
    terms = [term for term in sorted(AREA_RISK_EVIDENCE_TERMS) if term in normalized]
    return terms[:limit]


def _split_area_risk_label_candidate(value: str) -> List[str]:
    parts = re.split(r"\s*(?:,|;|/|\band\b|\bor\b|&)\s*", value)
    return [part.strip(" .:-()[]{}") for part in parts if part.strip(" .:-()[]{}")]


def _area_risk_label_candidates_from_text(text: str) -> List[str]:
    candidates: List[str] = []
    preposition_pattern = re.compile(
        r"\b(?:in|near|around|at|from|across|through|within|outside)\s+"
        r"([A-Z][A-Za-z0-9'’.-]*(?:\s+(?:of|the|de|del|la|le|du|da|do|dos|das|van|von|[A-Z][A-Za-z0-9'’.-]*)){0,4})"
    )
    title_pattern = re.compile(
        r"\b([A-Z][A-Za-z0-9'’.-]*(?:\s+(?:of|the|de|del|la|le|du|da|do|dos|das|van|von|[A-Z][A-Za-z0-9'’.-]*)){0,4})"
    )
    for pattern in [preposition_pattern, title_pattern]:
        for match in pattern.finditer(str(text or "")):
            for candidate in _split_area_risk_label_candidate(match.group(1)):
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def _clean_area_risk_label_candidate(label: str, context_labels: set[str]) -> Optional[str]:
    cleaned = " ".join(str(label or "").replace("’", "'").split()).strip(" .:-()[]{}")
    if not cleaned or len(cleaned) < 3 or len(cleaned) > 80:
        return None
    key = cleaned.casefold()
    if key in context_labels:
        return None
    words = [word.strip("'-.").casefold() for word in cleaned.split() if word.strip("'-.")]
    if not words or all(word in AREA_RISK_LABEL_STOPWORDS for word in words):
        return None
    if words[0] in AREA_RISK_LABEL_STOPWORDS:
        return None
    if any(fragment in key for fragment in {" public-safety watch", " aoi ", " risk area", " route planning"}):
        return None
    if len(words) > 5:
        return None
    return cleaned


def fallback_safe_route_area_risk_candidates(
    *,
    aoi: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    max_zones: int,
) -> List[Dict[str, Any]]:
    """Token-free fallback that extracts named, source-backed localities from bounded evidence."""

    bounded_evidence = _bounded_area_risk_evidence(evidence)
    context_labels = _area_risk_context_labels(aoi)
    candidates: Dict[str, Dict[str, Any]] = {}

    for item in bounded_evidence:
        url = _trim_text(item.get("url"), 400)
        if not url:
            continue
        title = _trim_text(item.get("title"), 180)
        snippet = _trim_text(item.get("snippet"), 420)
        searchable = " ".join(part for part in [title, snippet] if part)
        if not _area_risk_text_has_term(searchable):
            continue
        risk_terms = _area_risk_terms_in_text(searchable)
        for raw_label in _area_risk_label_candidates_from_text(searchable):
            label = _clean_area_risk_label_candidate(raw_label, context_labels)
            if not label:
                continue
            key = label.casefold()
            record = candidates.setdefault(
                key,
                {
                    "label": label,
                    "risk_terms": set(),
                    "evidence_urls": [],
                    "mentions": 0,
                },
            )
            record["mentions"] += 1
            record["risk_terms"].update(risk_terms)
            if url not in record["evidence_urls"]:
                record["evidence_urls"].append(url)

    ranked = sorted(
        candidates.values(),
        key=lambda item: (len(item["evidence_urls"]), item["mentions"], len(item["risk_terms"]), len(item["label"])),
        reverse=True,
    )
    zones: List[Dict[str, Any]] = []
    for item in ranked[: _bounded_area_risk_max_zones(max_zones)]:
        terms = sorted(item["risk_terms"])[:5]
        score = min(84, 48 + len(item["evidence_urls"]) * 8 + min(item["mentions"], 4) * 4 + len(terms) * 2)
        severity = "high" if score >= 68 else "medium"
        zones.append({
            "label": item["label"],
            "severity": severity,
            "risk_score": score,
            "confidence": "source-backed",
            "display_color": "red" if severity == "high" else "orange",
            "icon": "warning",
            "notes": (
                f"Bounded public evidence mentions {item['label']} alongside "
                f"{', '.join(terms) if terms else 'public-safety risk terms'}."
            ),
            "evidence_urls": item["evidence_urls"][:8],
        })
    return zones


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


async def _chat_completion_request(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    timeout = max(10, int(settings.http_timeout))
    model_name = model or settings.model
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }
    if _uses_completion_token_limit(model_name):
        payload["max_completion_tokens"] = 900
    else:
        payload["temperature"] = 0.2
        payload["max_tokens"] = 900
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response: Optional[httpx.Response] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        request_payload = dict(payload)
        for _ in range(3):
            response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=request_payload)
            if response.status_code != 400:
                break

            body = response.text or ""
            retry_payload = dict(request_payload)
            if "max_tokens" in body:
                retry_payload.pop("max_tokens", None)
                retry_payload["max_completion_tokens"] = 900
            if "temperature" in body:
                retry_payload.pop("temperature", None)
            if retry_payload == request_payload:
                break
            request_payload = retry_payload

    if response is None:
        raise RuntimeError("No response received from OpenAI")
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI returned HTTP {response.status_code}: {response.text[:400]}")

    return response.json()


async def run_openai_analysis(messages: List[Dict[str, Any]], *, model: Optional[str] = None) -> str:
    data = await _chat_completion_request(messages, model=model)
    parsed_text = _extract_text_from_chat_response(data)
    if parsed_text:
        return parsed_text
    return "I couldn't produce a structured answer for this query yet."


async def run_openai_web_research(prompt: str, *, model: Optional[str] = None) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    timeout = max(20, int(settings.http_timeout))
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    context_size = str(settings.area_risk_search_context_size or "medium").strip().lower()
    if context_size not in {"low", "medium", "high"}:
        context_size = "medium"

    base_payload: Dict[str, Any] = {
        "model": model or settings.area_risk_model or settings.model,
        "input": prompt,
        "max_output_tokens": max(200, min(int(settings.area_risk_max_output_tokens or 700), 1400)),
    }
    if _uses_reasoning_effort(str(base_payload["model"])):
        effort = str(settings.area_risk_reasoning_effort or "low").strip().lower()
        if effort in {"none", "low", "medium", "high", "xhigh"}:
            base_payload["reasoning"] = {"effort": effort}

    tool_variants: List[List[Dict[str, Any]]] = [
        [{"type": "web_search", "search_context_size": context_size}],
        [{"type": "web_search_preview", "search_context_size": context_size}],
    ]
    last_error = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for tools in tool_variants:
            payload = dict(base_payload)
            payload["tools"] = tools
            try:
                response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
            except Exception as exc:
                last_error = str(exc)
                continue
            if response.status_code == 400 and "reasoning" in payload and "reasoning" in (response.text or "").lower():
                retry_payload = dict(payload)
                retry_payload.pop("reasoning", None)
                try:
                    response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=retry_payload)
                except Exception as exc:
                    last_error = str(exc)
                    continue
            if response.status_code < 400:
                data = response.json() if response.content else {}
                parsed_text = _extract_responses_text(data)
                if parsed_text:
                    return parsed_text
                last_error = "Responses API returned no text"
                continue
            last_error = f"Responses API returned HTTP {response.status_code}: {response.text[:400]}"
    raise RuntimeError(last_error or "Responses API web research failed")


async def run_openai_responses_analysis(prompt: str, *, model: Optional[str] = None) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    timeout = max(20, int(settings.http_timeout))
    model_name = model or settings.area_risk_model or settings.model
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model_name,
        "input": prompt,
        "max_output_tokens": max(200, min(int(settings.area_risk_max_output_tokens or 700), 1400)),
    }
    if _uses_reasoning_effort(str(model_name)):
        effort = str(settings.area_risk_reasoning_effort or "low").strip().lower()
        if effort in {"none", "low", "medium", "high", "xhigh"}:
            payload["reasoning"] = {"effort": effort}

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if response.status_code == 400 and "reasoning" in payload and "reasoning" in (response.text or "").lower():
            retry_payload = dict(payload)
            retry_payload.pop("reasoning", None)
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=retry_payload)

    if response.status_code >= 400:
        raise RuntimeError(f"Responses API returned HTTP {response.status_code}: {response.text[:400]}")
    data = response.json() if response.content else {}
    parsed_text = _extract_responses_text(data)
    if parsed_text:
        return parsed_text
    raise RuntimeError("Responses API returned no text")


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


def build_safe_route_area_risk_evidence_prompt(
    *,
    session_id: Optional[str],
    aoi: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    max_zones: int,
) -> str:
    bounded_max_zones = _bounded_area_risk_max_zones(max_zones)
    system_prompt = (
        "You are Lunar SafeRoute Area Risk Agent. "
        "Identify named public-safety area-risk zones for route planning from sanitized AOI metadata, public evidence, and web research. "
        "Do not infer anything from tenant identity, routes, waypoints, convoy details, or protected client data. "
        "Do not provide tactical attack guidance. "
        "Return strict JSON only."
    )
    payload = {
        "agent": {
            "name": "Lunar SafeRoute Area Risk Agent",
            "runtime": "lunar-agent",
            "mode": "sanitized-area-risk-research",
        },
        "sessionId": str(session_id or "").strip() or None,
        "aoi": aoi,
        "evidence": _bounded_area_risk_evidence(evidence),
        "maxZones": bounded_max_zones,
        "instructions": [
            "Research and return named localities only: townships, neighbourhoods, informal settlements, industrial areas, transit nodes, or police-recognised crime hotspots.",
            "Do not return broad city/county/province/country zones such as 'Cape Town public-safety watch' or generic AOI circles.",
            "Prioritise areas with current or recurring public evidence of violent crime, gang violence, hijacking/carjacking, robbery, extortion, kidnapping, unrest, or severe road-safety disruption.",
            "Do not include an area solely because it is poor, informal, high-density, lacks services, has sanitation issues, or is socially vulnerable.",
            "Each zone must include evidence_urls from supplied evidence or current public web sources.",
            "Prefer smaller locality-level centers with radius_m around 500-2500m. Use larger radii only for a named township/locality with a genuinely broad footprint.",
            "If you cannot identify specific named areas, return zones=[].",
            "If evidence is insufficient, return zones=[].",
            "Never include tenant IDs, route details, usernames, or operational/security-sensitive information.",
        ],
        "responseShape": {
            "zones": [
                {
                    "label": "short area name",
                    "severity": "low | medium | high | critical",
                    "risk_score": "integer 0-100",
                    "confidence": "source-backed | modelled | analyst-reviewed",
                    "lat": "optional center latitude",
                    "lon": "optional center longitude",
                    "radius_m": "optional radius in meters",
                    "coordinates": [{"lat": "number", "lon": "number"}],
                    "display_color": "green | orange | red",
                    "icon": "warning | building | shield | alert",
                    "notes": "brief non-sensitive public-evidence summary naming the risk pattern",
                    "evidence_urls": ["public source URL"],
                }
            ],
            "notes": "brief processing note",
        },
    }
    return f"SYSTEM:\n{system_prompt}\n\nUSER:\n{json.dumps(payload, ensure_ascii=False)}"


def build_safe_route_area_risk_web_prompt(
    *,
    session_id: Optional[str],
    aoi: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    max_zones: int,
) -> str:
    bounded_max_zones = _bounded_area_risk_max_zones(max_zones)
    payload = {
        "agent": {
            "name": "Lunar SafeRoute Area Risk Agent",
            "runtime": "lunar-agent",
            "mode": "dynamic-public-area-risk-research",
        },
        "sessionId": str(session_id or "").strip() or None,
        "aoi": aoi,
        "seedEvidence": _bounded_area_risk_evidence(evidence),
        "maxZones": bounded_max_zones,
        "task": (
            "Research current and recurring public-safety area risks inside or near this AOI for SafeRoute. "
            "Return named townships, neighbourhoods, informal settlements, industrial areas, transit nodes, or police-recognised crime hotspots. "
            "Do not return a broad city-wide/province-wide/country-wide AOI summary."
        ),
        "mustDo": [
            "Use web research to identify specific named localities with public evidence.",
            "Focus especially on townships, high-crime areas, gang-affected localities, hijacking/carjacking hotspots, robbery/extortion hotspots, and unrest-prone areas.",
            "Only include informal settlements or deprived areas when public sources connect that named place to crime, violence, unrest, hijacking, robbery, extortion, or other direct public-safety risk.",
            "Keep each zone small and locality-specific. radius_m should usually be 500-2500.",
            "Include public source URLs for every zone.",
            "Use approximate public-safety mapping only. Do not include tactical attack guidance or operational advice.",
            "If the evidence supports a larger named township, use its approximate center and a radius that covers the township, not the whole AOI.",
            "If you cannot identify named localities, return an empty zones array.",
        ],
        "mustNotDo": [
            "Do not output labels like 'Cape Town public-safety watch', 'Western Cape risk area', or 'AOI risk zone'.",
            "Do not create generic circles around the route or AOI center.",
            "Do not include an area solely because it is poor, informal, high-density, lacks services, has sanitation issues, or is socially vulnerable.",
            "Do not mention tenant, route, convoy, client, user, or waypoint details.",
            "Do not invent precise boundaries when only public article-level evidence exists.",
        ],
        "responseShape": {
            "zones": [
                {
                    "label": "named locality, e.g. township/neighbourhood/hotspot",
                    "severity": "low | medium | high | critical",
                    "risk_score": "integer 0-100",
                    "confidence": "source-backed | modelled | analyst-reviewed",
                    "lat": "center latitude if known",
                    "lon": "center longitude if known",
                    "radius_m": "500-2500 for most named localities",
                    "coordinates": [{"lat": "number", "lon": "number"}],
                    "display_color": "green | orange | red",
                    "icon": "warning | building | shield | alert",
                    "notes": "brief non-sensitive summary of the public risk pattern and why this named area was included",
                    "evidence_urls": ["public source URL"],
                }
            ],
            "notes": "brief processing note",
        },
    }
    return (
        "You are Lunar SafeRoute Area Risk Agent. Return strict JSON only.\n"
        "Identify specific named public-safety risk localities from public web research.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def normalize_safe_route_area_risk_payload(payload: Dict[str, Any], max_zones: int) -> Dict[str, Any]:
    zones: List[Dict[str, Any]] = []
    raw_zones = payload.get("zones") if isinstance(payload, dict) else []
    if not isinstance(raw_zones, list):
        raw_zones = []

    for raw_zone in raw_zones:
        if not isinstance(raw_zone, dict):
            continue
        label = _trim_text(raw_zone.get("label") or raw_zone.get("name") or raw_zone.get("title"), 120)
        if not label:
            continue
        severity = str(raw_zone.get("severity") or "medium").strip().lower()
        if severity not in {"low", "medium", "high", "critical"}:
            severity = "medium"
        confidence = str(raw_zone.get("confidence") or "source-backed").strip().lower()
        if confidence not in {"source-backed", "modelled", "analyst-reviewed"}:
            confidence = "source-backed"
        display_color = str(raw_zone.get("display_color") or raw_zone.get("displayColor") or "").strip().lower()
        if display_color not in {"green", "orange", "red"}:
            display_color = "red" if severity in {"high", "critical"} else "orange" if severity == "medium" else "green"
        evidence_urls = _normalize_text_list(
            raw_zone.get("evidence_urls") or raw_zone.get("evidenceUrls"),
            max_items=8,
            max_len=400,
        )
        zones.append({
            "label": label,
            "severity": severity,
            "risk_score": _coerce_bounded_int(raw_zone.get("risk_score") or raw_zone.get("riskScore"), 45, 0, 100),
            "confidence": confidence,
            "lat": _coerce_bounded_float(raw_zone.get("lat"), -90, 90),
            "lon": _coerce_bounded_float(raw_zone.get("lon") if raw_zone.get("lon") is not None else raw_zone.get("lng"), -180, 180),
            "radius_m": _coerce_bounded_float(raw_zone.get("radius_m") or raw_zone.get("radiusM"), 0, 100000),
            "coordinates": _normalize_area_risk_coordinates(raw_zone.get("coordinates")),
            "display_color": display_color,
            "icon": _trim_text(raw_zone.get("icon"), 80) or "warning",
            "notes": _trim_text(raw_zone.get("notes"), 1200),
            "evidence_urls": evidence_urls,
        })
        if len(zones) >= max(1, min(int(max_zones or 8), 20)):
            break

    return {
        "zones": zones,
        "notes": _trim_text(payload.get("notes") if isinstance(payload, dict) else None, 600),
    }


async def research_safe_route_area_risk(
    *,
    session_id: Optional[str],
    aoi: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    max_zones: int = 8,
) -> Dict[str, Any]:
    bounded_max_zones = _bounded_area_risk_max_zones(max_zones)
    if settings.area_risk_web_research_enabled:
        web_prompt = build_safe_route_area_risk_web_prompt(
            session_id=session_id,
            aoi=aoi,
            evidence=evidence,
            max_zones=bounded_max_zones,
        )
        try:
            raw_answer = await run_openai_web_research(web_prompt, model=settings.area_risk_model)
            parsed = _safe_parse_json_object(raw_answer) or {"zones": []}
            normalized = normalize_safe_route_area_risk_payload(parsed, max_zones=bounded_max_zones)
            normalized["model"] = settings.area_risk_model
            normalized["notes"] = normalized.get("notes") or "Dynamic public web research completed."
            if normalized.get("zones"):
                return normalized
        except Exception as exc:
            logger.warning(
                "Area-risk web research failed; falling back to supplied evidence only: %s",
                exc,
                exc_info=True,
            )
            fallback_note = "Dynamic web research failed; fell back to supplied evidence only."
            if not settings.area_risk_fallback_on_web_error:
                return {"zones": [], "model": settings.area_risk_model, "notes": fallback_note}
        else:
            fallback_note = "Dynamic web research returned no named locality zones; checked supplied evidence fallback."
            if not settings.area_risk_fallback_on_empty_web:
                return {
                    "zones": [],
                    "model": settings.area_risk_model,
                    "notes": "Dynamic web research returned no named locality zones.",
                }
    else:
        fallback_note = "Dynamic web research disabled; used supplied evidence only."

    fallback_candidates = fallback_safe_route_area_risk_candidates(
        aoi=aoi,
        evidence=evidence,
        max_zones=bounded_max_zones,
    )
    if fallback_candidates:
        normalized_fallback = normalize_safe_route_area_risk_payload(
            {"zones": fallback_candidates},
            max_zones=bounded_max_zones,
        )
        normalized_fallback["model"] = settings.area_risk_model
        normalized_fallback["notes"] = "Used bounded public evidence fallback for named locality candidates."
        return normalized_fallback

    try:
        evidence_prompt = build_safe_route_area_risk_evidence_prompt(
            session_id=session_id,
            aoi=aoi,
            evidence=evidence,
            max_zones=bounded_max_zones,
        )
        raw_answer = await run_openai_responses_analysis(evidence_prompt, model=settings.area_risk_model)
        parsed = _safe_parse_json_object(raw_answer) or {"zones": []}
        normalized = normalize_safe_route_area_risk_payload(parsed, max_zones=bounded_max_zones)
    except Exception as exc:
        logger.warning("Area-risk evidence analysis failed; using deterministic evidence fallback: %s", exc)
        normalized = {"zones": [], "notes": ""}
    normalized["model"] = settings.area_risk_model
    normalized["notes"] = normalized.get("notes") or fallback_note
    return normalized


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
