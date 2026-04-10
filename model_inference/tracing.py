from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

_TRACE_LOCK = threading.Lock()


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if any(token in key_lower for token in ("authorization", "api_key", "token", "secret", "password")):
                out[key_text] = "***REDACTED***"
            else:
                out[key_text] = _sanitize(nested)
        return out
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value


def _trace_file_for_role(role: str) -> Path | None:
    trace_dir = str(os.getenv("ACEBENCH_TRACE_DIR", "")).strip()
    if not trace_dir:
        return None
    safe_role = role.strip().lower().replace("/", "_") or "unknown"
    return Path(trace_dir).expanduser().resolve() / f"{safe_role}.jsonl"


def _append_trace(role: str, payload: Mapping[str, Any]) -> None:
    trace_path = _trace_file_for_role(role)
    if trace_path is None:
        return
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "role": role,
        **_sanitize(dict(payload)),
    }
    line = json.dumps(row, ensure_ascii=False)
    with _TRACE_LOCK:
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


def _extract_response_text(response: Any) -> str:
    try:
        choices = getattr(response, "choices", None)
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = getattr(first, "message", None)
            content = getattr(message, "content", None)
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
                if parts:
                    return "".join(parts)
    except Exception:
        return ""
    return ""


def traced_chat_completion(
    *,
    client: Any,
    role: str,
    model: str,
    messages: list[dict[str, Any]],
    context: Mapping[str, Any] | None = None,
    **request_kwargs: Any,
) -> Any:
    request_payload = {
        "model": model,
        "messages": messages,
        **request_kwargs,
    }
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            **request_kwargs,
        )
        duration_ms = (time.perf_counter() - started) * 1000.0
        _append_trace(
            role,
            {
                "status": "ok",
                "duration_ms": round(duration_ms, 3),
                "request": request_payload,
                "response_text": _extract_response_text(response),
                "context": dict(context or {}),
            },
        )
        return response
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000.0
        _append_trace(
            role,
            {
                "status": "error",
                "duration_ms": round(duration_ms, 3),
                "request": request_payload,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "context": dict(context or {}),
            },
        )
        raise
