"""Thin wrapper around the Meta Model API (Muse Spark): file input, structured output, tool calling.

Uses the Responses API over plain HTTP (POST {META_BASE_URL}/responses), per https://dev.meta.ai/docs/:
  - structured output: `text.format = {"type": "json_schema", "name", "schema", "strict"}` (flat)
  - files: `input_file` (PDF as a base64 data URL) and `input_image` parts in the user message
  - tools: flat function tools; we answer `function_call` items with `function_call_output`
    items, resending the history ourselves instead of relying on stored responses.
Standard tier only; never send real school data on the contributor tier.
"""

import base64
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
TEXT_SUFFIXES = {".txt", ".md", ".csv"}

# Running token totals since the server started, shown on /api/health for the cost check.
usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
_usage_lock = threading.Lock()  # cards are built on several threads at once


class MetaError(RuntimeError):
    pass


@dataclass
class Tool:
    """A function the model may call. `fn` gets the parsed arguments and returns JSON-able data."""

    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], Any]


def configured() -> bool:
    return bool(settings.meta_api_key)


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(
        base_url=settings.meta_base_url,
        headers={"Authorization": f"Bearer {settings.meta_api_key}"},
        timeout=180,
    )


def json_schema(model: type[BaseModel]) -> dict:
    """Pydantic's schema, cleaned up for the API: $refs inlined, titles/defaults dropped, and every
    object closed (`additionalProperties: false`) with all of its properties required."""
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return clean(defs[node["$ref"].split("/")[-1]])
        out: dict = {}
        for key, value in node.items():
            if key in ("title", "default", "$defs"):
                continue
            if key == "properties":
                out[key] = {name: clean(sub) for name, sub in value.items()}
            else:
                out[key] = clean(value)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return clean(raw)


def file_part(path: Path) -> dict:
    """One input content part for a local file."""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")[:100_000]
        return {"type": "input_text", "text": f"--- {path.name} ---\n{text}"}
    mime = MIME.get(suffix)
    if mime is None:
        raise MetaError(f"Can't send {path.name} to the model (convert it to PDF first)")
    data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
    if mime.startswith("image/"):
        return {"type": "input_image", "image_url": data_url, "detail": "high"}
    return {"type": "input_file", "filename": path.name, "file_data": data_url}


def output_text(response: dict) -> str:
    """Join the `output_text` parts of every message item (the raw API has no output_text field)."""
    texts = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "refusal":
                raise MetaError(f"Model refused: {part.get('refusal', '')}")
    return "".join(texts)


def _post(client: httpx.Client, body: dict) -> dict:
    r = client.post("/responses", json=body)
    if r.status_code >= 400:
        raise MetaError(f"Meta API {r.status_code}: {r.text[:500]}")
    data = r.json()
    if data.get("error"):
        raise MetaError(f"Meta API error: {data['error']}")
    if data.get("status") == "incomplete":
        raise MetaError(f"Response incomplete: {data.get('incomplete_details')}")
    u = data.get("usage") or {}
    with _usage_lock:
        usage["calls"] += 1
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)
    log.info("meta call: in=%s out=%s", u.get("input_tokens"), u.get("output_tokens"))
    return data


def _run_tool(tools: dict[str, Tool], call: dict) -> str:
    tool = tools.get(call.get("name", ""))
    if tool is None:
        return json.dumps({"error": f"unknown tool {call.get('name')}"})
    try:
        args = json.loads(call.get("arguments") or "{}")
        result = tool.fn(args if isinstance(args, dict) else {})
    except Exception as e:  # report to the model instead of failing the whole card
        log.warning("tool %s failed: %s", tool.name, e)
        return json.dumps({"error": str(e)})
    return json.dumps(result, default=str)[:20_000]


def _parse(text: str, schema: type[T]) -> T:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        raise MetaError(f"Reply didn't match {schema.__name__}: {e.errors()[:3]} in {text[:300]!r}")


def generate_structured(
    prompt: str,
    schema: type[T],
    files: list[Path] | None = None,
    instructions: str | None = None,
    tools: list[Tool] | None = None,
    max_turns: int = 6,
) -> T:
    """Send `prompt` (+ optional PDFs/images) and return the reply parsed into `schema`.

    With `tools`, runs the tool-calling loop: whenever the model asks for a function, call it and
    send the result back, until the model replies with the structured answer.
    """
    if not configured():
        raise MetaError("META_API_KEY is not set")
    content = [{"type": "input_text", "text": prompt}] + [file_part(f) for f in files or []]
    history: list[dict] = [{"role": "user", "content": content}]
    body: dict = {
        "model": settings.meta_model,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema.__name__,
                "schema": json_schema(schema),
                "strict": False,
            }
        },
    }
    if instructions:
        body["instructions"] = instructions
    by_name = {t.name: t for t in tools or []}
    if tools:
        body["tools"] = [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ]

    with _client() as client:
        for _ in range(max_turns):
            response = _post(client, {**body, "input": history})
            calls = [i for i in response.get("output") or [] if i.get("type") == "function_call"]
            if not calls:
                return _parse(output_text(response), schema)
            for call in calls:
                history.append(
                    {
                        "type": "function_call",
                        "call_id": call["call_id"],
                        "name": call["name"],
                        "arguments": call.get("arguments") or "{}",
                    }
                )
            for call in calls:
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": _run_tool(by_name, call),
                    }
                )
    raise MetaError(f"No answer after {max_turns} tool-call rounds")
