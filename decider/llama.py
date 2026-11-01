"""HTTP client for llama-server running Muse Glimmer. Ported from glimmer_check.py's Server, with
token-array prompts so generated thinking goes back exactly as the ids the server returned.

Every prompt is sent as token ids. The first piece is tokenized with BOS (a token-array prompt gets
no BOS from the server), and every later piece starts with a special token, so pieces join without
the tokenizer merging anything across the seam.
"""

import re
import threading
import time
from collections import Counter
from pathlib import Path

import httpx

MIN_BUILD = 10353  # first llama.cpp release that knows the muse-glimmer architecture
BOS = "<|begin_of_text|>"


class LlamaError(RuntimeError):
    """llama-server is unreachable or answered with an error."""


def build_number(build: str) -> int:
    match = re.match(r"b(\d+)", build or "")
    return int(match.group(1)) if match else 0


class Llama:
    def __init__(self, url: str, http: httpx.Client | None = None, timeout: float = 300) -> None:
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)
        self._consts: dict[str, list[int]] = {}
        self._props: dict | None = None
        self._lock = threading.Lock()
        self.counts: Counter = Counter()  # requests, prompt tokens processed, tokens generated
        self.log: list[dict] | None = None  # set to [] to record every /completion call

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self.http.post(self.url + path, json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LlamaError(f"{path}: {e}") from e
        return r.json()

    def props(self) -> dict:
        if self._props is None:
            try:
                r = self.http.get(self.url + "/props", timeout=10)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LlamaError(f"/props: {e}") from e
            self._props = r.json()
        return self._props

    def healthy(self) -> bool:
        try:
            return self.http.get(self.url + "/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def build(self) -> str:
        return self.props().get("build_info", "")

    def model(self) -> str:
        """The model file's name without folder or extension."""
        return Path(self.props().get("model_alias") or self.props().get("model_path") or "").stem

    def tokenize(self, text: str, bos: bool = False) -> list[int]:
        body = {"content": text, "add_special": bos, "parse_special": True}
        return self._post("/tokenize", body)["tokens"]

    def detokenize(self, ids: list[int]) -> str:
        return self._post("/detokenize", {"tokens": ids})["content"]

    def const(self, text: str) -> list[int]:
        """Token ids of a fixed piece of prompt (no BOS), tokenized once."""
        with self._lock:
            if text not in self._consts:
                self._consts[text] = self.tokenize(text)
            return self._consts[text]

    def special(self, text: str) -> int:
        ids = self.const(text)
        if len(ids) != 1:
            raise LlamaError(f"{text} is not one token: {ids}")
        return ids[0]

    def render(self, messages: list[dict]) -> str:
        """The chat template's text for these turns, without BOS and without a generation prompt."""
        body = {"messages": messages, "add_generation_prompt": False}
        text = self._post("/apply-template", body)["prompt"]
        return text.removeprefix(BOS).removesuffix("<|start|>assistant")

    def completion(self, body: dict, kind: str) -> dict:
        start = time.perf_counter()
        resp = self._post("/completion", body)
        t = resp.get("timings", {})
        with self._lock:
            self.counts[kind] += 1
            self.counts["prompt_n"] += t.get("prompt_n", 0)
            self.counts["predicted_n"] += t.get("predicted_n", 0)
        if self.log is not None:
            self.log.append({
                "kind": kind,
                "slot": body.get("id_slot", 0),
                "prompt": list(body["prompt"]),
                "tokens": resp.get("tokens") or [],
                "prompt_n": t.get("prompt_n"),
                "cache_n": t.get("cache_n"),
                "ms": (time.perf_counter() - start) * 1000,
            })
        return resp
