"""Meta client against a fake Responses API: schema shape, structured output, the tool loop."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app import meta_client
from app.config import settings
from app.meta_client import MetaError, Tool


class Inner(BaseModel):
    title: str  # a property literally named "title" must survive the cleanup
    n: int = 3


class Outer(BaseModel):
    items: list[Inner]
    note: str | None = None


def reply(text):
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
def fake_api(monkeypatch):
    """Queue of responses to hand back, and the request bodies that were sent."""
    queue, sent = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    object.__setattr__(settings, "meta_api_key", "key")
    monkeypatch.setattr(
        meta_client,
        "_client",
        lambda: httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler)),
    )
    return queue, sent


def test_json_schema_is_closed_and_inlined():
    s = meta_client.json_schema(Outer)
    assert "$defs" not in json.dumps(s) and "$ref" not in json.dumps(s)
    assert s["additionalProperties"] is False and s["required"] == ["items", "note"]
    inner = s["properties"]["items"]["items"]
    assert inner["required"] == ["title", "n"] and "title" in inner["properties"]
    assert "default" not in json.dumps(s)


def test_structured_output(fake_api):
    queue, sent = fake_api
    queue.append(reply('{"items": [{"title": "a", "n": 1}], "note": null}'))
    out = meta_client.generate_structured("hi", Outer, instructions="be brief")
    assert out.items[0].title == "a"
    body = sent[0]
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["name"] == "Outer"
    assert body["instructions"] == "be brief"
    assert body["input"][0]["content"][0] == {"type": "input_text", "text": "hi"}


def test_tool_loop_sends_results_back(fake_api):
    queue, sent = fake_api
    queue.append(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q": "x"}'}],
        }
    )
    queue.append(reply('{"items": [], "note": "done"}'))
    seen = []
    tool = Tool("lookup", "find things", {"type": "object", "properties": {"q": {"type": "string"}}}, lambda a: seen.append(a) or {"found": 1})

    out = meta_client.generate_structured("go", Outer, tools=[tool])
    assert out.note == "done" and seen == [{"q": "x"}]
    assert sent[0]["tools"][0]["name"] == "lookup" and sent[0]["tools"][0]["type"] == "function"
    second = sent[1]["input"]
    assert second[1] == {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q": "x"}'}
    assert second[2] == {"type": "function_call_output", "call_id": "call_1", "output": '{"found": 1}'}


def test_errors(fake_api):
    queue, _ = fake_api
    queue.append(reply("not json"))
    with pytest.raises(MetaError):
        meta_client.generate_structured("x", Outer)
    object.__setattr__(settings, "meta_api_key", "")
    with pytest.raises(MetaError):
        meta_client.generate_structured("x", Outer)


def test_file_parts(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    img = tmp_path / "b.jpg"
    img.write_bytes(b"\xff\xd8")
    assert meta_client.file_part(pdf)["type"] == "input_file"
    assert meta_client.file_part(pdf)["file_data"].startswith("data:application/pdf;base64,")
    assert meta_client.file_part(img)["image_url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(MetaError):
        meta_client.file_part(tmp_path / "c.docx")
