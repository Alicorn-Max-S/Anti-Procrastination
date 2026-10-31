"""Reading plans: how long a reading really is and where to stop, from the file's own text.

Works on the document reader's book pages (reader.Doc.book_pages: printed page number + text,
two-page scans already split). Page numbers missing on some pages (a chapter opener) are filled in
from their neighbours. Section headings are short Title Case lines with body text around them;
whole sections are grouped into sittings of about SITTING_MINUTES, so the card can say
"Read pp. 177–191 (about 19 min)" with real numbers instead of the teacher's "roughly 30pp". If the instructions name a page range ("pp. 181-195"), only that range is planned.
"""

import math
import re
from collections import Counter
from typing import Protocol

from app.models import ReadingPlan, ReadingSitting

WPM = 180  # a high-school student reading dense nonfiction, pencil in hand
SITTING_MINUTES = 25
MAX_SITTINGS = 5
MIN_WORDS = 1200  # shorter than this isn't worth a plan
MIN_PAGE_WORDS = 60  # a leading/trailing page with less text than this is front or back matter
OPENING = "the opening"
OPENING_WORDS = 18  # words of each section quoted to the model


class Page(Protocol):
    number: str | None
    text: str


# ---- page numbers -----------------------------------------------------------------------------


def _numbers(pages: list[Page]) -> list[int] | None:
    """Printed number for every page: the offset most numbered pages agree on, applied to all.
    None when the pages carry no usable numbers."""
    offsets = Counter(int(p.number) - i for i, p in enumerate(pages) if p.number and p.number.isdigit())
    if not offsets:
        return None
    offset, votes = offsets.most_common(1)[0]
    if votes < 2 and len(pages) > 2:
        return None
    return [i + offset for i in range(len(pages))]


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{2,}", text))


# ---- headings ---------------------------------------------------------------------------------

_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "vs", "with"}


def _is_heading(line: str) -> bool:
    words = line.split()
    if not 2 <= len(words) <= 9 or len(line) > 70:
        return False
    if re.search(r"[.,;!?\"'”’\-•)\]]$", line) or re.search(r"\d{2,}", line):
        return False
    letters = [w for w in words if re.match(r"[A-Za-z]{3,}", w)]
    if len(letters) < 2 or line.isupper():
        return False
    big = [w for w in letters if w.lower() not in _SMALL]
    return bool(big) and words[0][0].isupper() and all(w[0].isupper() for w in big)


def _body(line: str) -> bool:
    return len(line.split()) >= 8


def _running_headers(pages: list[Page]) -> set[str]:
    """Lines that repeat across pages (running heads), with page numbers stripped."""
    seen: Counter = Counter()
    for p in pages:
        lines = [l.strip() for l in p.text.splitlines() if l.strip()]
        for line in set(lines[:2] + lines[-1:]):
            seen[re.sub(r"^\d+\s+|\s+\d+$", "", line)] += 1
    return {line for line, n in seen.items() if n >= 3}


# ---- sections and sittings --------------------------------------------------------------------


class _Section:
    def __init__(self, title: str, first: int) -> None:
        self.title = title
        self.first = first
        self.last = first
        self.words = 0
        self.opening = ""  # its first words, so the model can tell what each sitting covers


def _sections(pages: list[Page], numbers: list[int]) -> list[_Section]:
    heads = _running_headers(pages)
    sections: list[_Section] = []
    for k, (page, number) in enumerate(zip(pages, numbers)):
        lines = [l.strip() for l in page.text.splitlines() if l.strip()]
        seen_body = False
        for j, line in enumerate(lines):
            if re.sub(r"^\d+\s+|\s+\d+$", "", line) in heads and (j < 2 or j == len(lines) - 1):
                continue
            if _is_heading(line) and (seen_body or k > 0) and any(_body(l) for l in lines[j + 1 : j + 4]):
                sections.append(_Section(line, number))
                continue
            if _body(line):
                seen_body = True
            if not sections:  # text before the first heading: the reading's opening
                sections.append(_Section(OPENING, number))
            sections[-1].words += _words(line)
            sections[-1].last = number
            if len(sections[-1].opening.split()) < OPENING_WORDS and _body(line):
                joined = (sections[-1].opening + " " + line).replace("­ ", "")  # soft-hyphen line breaks
                sections[-1].opening = " ".join(joined.split()[:OPENING_WORDS])
    return [s for s in sections if s.words]


def _split_long(sections: list[_Section], page_words: dict[int, int]) -> list[_Section]:
    """Cut a section much longer than a sitting into parts at page breaks."""
    limit = SITTING_MINUTES * WPM * 1.5
    out: list[_Section] = []
    for s in sections:
        if s.words <= limit or s.last == s.first:
            out.append(s)
            continue
        parts = math.ceil(s.words / (SITTING_MINUTES * WPM))
        per = s.words / parts
        part, n = _Section(f"{s.title} (part 1)", s.first), 1
        for page in range(s.first, s.last + 1):
            if part.words >= per and n < parts:
                out.append(part)
                n += 1
                part = _Section(f"{s.title} (part {n})", page)
            part.words += page_words.get(page, 0)
            part.last = page
        out.append(part)
    return out


def _partition(sizes: list[int], k: int) -> list[int]:
    """Indexes where each of k contiguous groups starts, minimising the largest group."""
    n = len(sizes)
    prefix = [0]
    for s in sizes:
        prefix.append(prefix[-1] + s)
    best = [[math.inf] * (k + 1) for _ in range(n + 1)]
    cut = [[0] * (k + 1) for _ in range(n + 1)]
    best[0][0] = 0
    for i in range(1, n + 1):
        for g in range(1, min(i, k) + 1):
            for j in range(g - 1, i):
                cost = max(best[j][g - 1], prefix[i] - prefix[j])
                if cost < best[i][g]:
                    best[i][g], cut[i][g] = cost, j
    starts, i = [], n
    for g in range(k, 0, -1):
        i = cut[i][g]
        starts.append(i)
    return starts[::-1]


def page_range(text: str) -> tuple[int, int] | None:
    """A page range the instructions assign, e.g. "pp. 181-195" or "pages 12 to 30"."""
    m = re.search(r"\b(?:pp?\.|pages?)\s*(\d{1,4})\s*(?:-|–|—|to|through)\s*(\d{1,4})\b", text, re.I)
    if m and int(m.group(1)) < int(m.group(2)):
        return int(m.group(1)), int(m.group(2))
    return None


def plan(pages: list[Page], file: str, assigned: tuple[int, int] | None = None) -> ReadingPlan | None:
    """The reading plan for a document's book pages, or None if it's too short to need one."""
    numbers = _numbers(pages) or list(range(1, len(pages) + 1))
    pairs = list(zip(pages, numbers))
    if assigned:
        inside = [(p, n) for p, n in pairs if assigned[0] <= n <= assigned[1]]
        pairs = inside or pairs
    while pairs and _words(pairs[0][0].text) < MIN_PAGE_WORDS and not pairs[0][0].number:
        pairs.pop(0)
    while pairs and _words(pairs[-1][0].text) < MIN_PAGE_WORDS and not pairs[-1][0].number:
        pairs.pop()
    if not pairs:
        return None
    pages, numbers = [p for p, _ in pairs], [n for _, n in pairs]
    words = sum(_words(p.text) for p in pages)
    if words < MIN_WORDS:
        return None

    sections = _split_long(_sections(pages, numbers), {n: _words(p.text) for p, n in pairs})
    words = sum(s.words for s in sections)  # body text only: no running heads or headings
    minutes = max(1, round(words / WPM))
    k = max(1, min(MAX_SITTINGS, len(sections), math.ceil(minutes / SITTING_MINUTES)))
    starts = _partition([s.words for s in sections], k) + [len(sections)]
    pdf_of = {n: getattr(p, "pdf_page", None) for p, n in pairs}  # reader.BookPage has it
    sittings = []
    for a, b in zip(starts, starts[1:]):
        group = sections[a:b]
        group_words = sum(s.words for s in group)
        first, last = pdf_of.get(group[0].first), pdf_of.get(group[-1].last)
        pdf = "" if first is None or (first, last) == (group[0].first, group[-1].last) else (
            str(first) if first == last else f"{first}-{last}"
        )
        sittings.append(
            ReadingSitting(
                first_page=str(group[0].first),
                last_page=str(group[-1].last),
                sections=[s.title for s in group],
                words=group_words,
                minutes=max(1, round(group_words / WPM)),
                pdf_pages=pdf,
                openings=[s.opening for s in group],
            )
        )
    return ReadingPlan(
        file=file, first_page=str(numbers[0]), last_page=str(numbers[-1]), pages=len(pages),
        words=words, minutes=minutes, sittings=sittings,
    )


# ---- wording for the card ---------------------------------------------------------------------


def pages_text(first: str, last: str) -> str:
    return f"p. {first}" if first == last else f"pp. {first}–{last}"


_SECOND_CLAUSE = re.compile(r",?\s+and\s+(?:how|why|what|where|when|whether)\s+", re.I)


def short_note(note: str, max_words: int = 14) -> str:
    """A "Look for" note cut back to its first clause when it runs long ("Look for how X, and how
    Y" keeps "Look for how X.")."""
    note = " ".join(note.split())
    if len(note.split()) <= max_words:
        return note
    first = _SECOND_CLAUSE.split(note, maxsplit=1)[0].rstrip(" ,;.")
    return f"{first}." if first != note.rstrip(" ,;.") else note


def step(sitting: ReadingSitting, note: str = "") -> str:
    """"Read pp. 187–198 (about 20 min). Look for ...": pages and minutes, the note kept short.
    The sections stay in the plan; naming them here made each step two long sentences."""
    line = f"Read {pages_text(sitting.first_page, sitting.last_page)} (about {sitting.minutes} min)."
    note = short_note(note)
    return f"{line} {note}" if note else line


def summary_line(p: ReadingPlan) -> str:
    """"35 pages (pp. 177–211), about 65 minutes of reading in 3 sittings.\""""
    sittings = f" in {len(p.sittings)} sittings" if len(p.sittings) > 1 else ""
    return f"{p.pages} pages ({pages_text(p.first_page, p.last_page)}), about {p.minutes} minutes of reading{sittings}."


def prompt_block(p: ReadingPlan) -> str:
    lines = [f"{p.file}: {summary_line(p)}"]
    for i, s in enumerate(p.sittings, 1):
        pdf = f" (PDF pages {s.pdf_pages})" if s.pdf_pages else ""
        lines.append(f"  Sitting {i}: {pages_text(s.first_page, s.last_page)}{pdf}, about {s.minutes} min. Sections:")
        for title, opening in zip(s.sections, s.openings or [""] * len(s.sections)):
            lines.append(f"    - {title}" + (f', starting "{opening} ..."' if opening else ""))
    return "\n".join(lines)
