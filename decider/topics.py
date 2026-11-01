"""Topic naming: when a question's topic is decided as "None of these fit", Glimmer names one.

A name that matches a listed topic (ignoring case, spaces and underscores) is that topic. Anything
else is a new topic, stored the way the grader spells topics (decisions.fields.normalize_topic).

The spec's question alone made Glimmer copy its first sentence back ("None of the listed topics
fit.") on 3 of 3 real questions, with the answer turn opened directly. With the reply line and the
fast pass's empty thinking turn it wrote a topic on all 3. An echo is still never made a topic.
"""

from decisions.fields import normalize_topic

from .llama import Llama
from .prompts import Prompt

NAME_QUESTION = (
    "None of the listed topics fit. Write a short topic name (6 words or fewer). If a listed topic "
    "is close, write that one exactly."
)
NAME_REPLY = "Reply with just the topic name."
MAX_WORDS = 6


def clean_name(text: str) -> str:
    name = text.strip().strip("\"'`*").strip().rstrip(".").strip()
    name = " ".join(name.split()[:MAX_WORDS])
    return "" if normalize_topic(name).startswith("none_of") else name  # an echo, not a name


def match_topic(name: str, topics: list[str]) -> str | None:
    key = normalize_topic(name)
    return next((t for t in topics if normalize_topic(t) == key), None)


def name_topic(llama: Llama, context: str, fast_format: str, slot: int = 0) -> str:
    """Glimmer's name for the topic (raw, cleaned), or "" if it wrote nothing usable."""
    prompt = Prompt(llama, context, f"Question: {NAME_QUESTION}\n{NAME_REPLY}", fast_format)
    body = {
        "prompt": prompt.answer([]),
        "n_predict": 16,
        "temperature": 0,
        "stop": ["\n"],
        "cache_prompt": True,
        "id_slot": slot,
    }
    return clean_name(llama.completion(body, kind="name").get("content", ""))


def resolve(name: str, topics: list[str]) -> tuple[str, bool]:
    """(the topic to use, whether it is new)."""
    existing = match_topic(name, topics)
    return (existing, False) if existing is not None else (normalize_topic(name), True)
