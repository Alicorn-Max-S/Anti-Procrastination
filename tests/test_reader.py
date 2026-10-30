"""Document reader: spreads split into book pages, the text cache, and what an assignment's links
turn into (Google files public / school-only / sniffed, articles, paywalls, videos)."""

import httpx
import pytest

from app import gdocs, reader, web
from app.models import Assignment


def make_pdf(pages: list[tuple[float, float, list[tuple[float, float, str]]]]) -> bytes:
    """A real (tiny) PDF: each page is (width, height, [(x, y, text), ...]) in Helvetica."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for width, height, runs in pages:
        stream = "".join(f"BT /F1 11 Tf {x} {y} Td ({text}) Tj ET\n" for x, y, text in runs).encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}endstream")
        content = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def spread(left_no: int) -> tuple[float, float, list]:
    """A scanned book spread: two pages side by side, each with a running head and some text."""
    body = "The network kept its secrets so well that very little was ever written down at all."
    return (792, 612, [
        (40, 570, f"{left_no} Chapter Seven"), (40, 540, body), (40, 520, body),
        (420, 570, f"The Unbreakable Chain {left_no + 1}"), (420, 540, body), (420, 520, body),
    ])


def test_spreads_become_book_pages_with_printed_numbers(tmp_path):
    path = tmp_path / "chain.pdf"
    path.write_bytes(make_pdf([spread(178), spread(180), spread(182)]))
    doc = reader.read_document(path)
    assert doc.readable and doc.pdf_pages == 3 and doc.real_pages == 6
    assert [p.number for p in doc.book_pages] == ["178", "179", "180", "181", "182", "183"]
    assert [p.pdf_page for p in doc.book_pages] == [1, 1, 2, 2, 3, 3]
    assert doc.book_pages[1].text.startswith("The Unbreakable Chain 179")
    assert "Chain" not in doc.book_pages[0].text  # the right page's text stays on the right
    assert reader.text_pages(path)[0][2].startswith("180 Chapter Seven")


def test_portrait_pages_and_line_numbers_arent_page_numbers(tmp_path):
    path = tmp_path / "poems.pdf"
    path.write_bytes(make_pdf([(612, 792, [(72, 700, "Power"), (72, 680, "5")]), (612, 792, [(72, 700, "15")])]))
    doc = reader.read_document(path)
    assert doc.real_pages == 2 and doc.pdf_pages == 2
    assert [p.number for p in doc.book_pages] == [None, None]  # 5 then 15 isn't a page sequence


def test_text_is_cached_next_to_the_file(tmp_path, monkeypatch):
    path = tmp_path / "chain.pdf"
    path.write_bytes(make_pdf([spread(10)]))
    first = reader.read_document(path)
    assert (tmp_path / "chain.pdf.reader.json").exists()
    monkeypatch.setattr(reader, "_read_uncached", lambda p: pytest.fail("read again instead of using the cache"))
    assert reader.read_document(path, name="The chain").real_pages == first.real_pages == 2


DOC = "https://docs.google.com/document/d/1PublicDocAbCdEfGhIjKlMn/edit"
LAB = "https://docs.google.com/document/d/1EmbeddedLabAbCdEfGhIjKl/preview"
DRIVE = "https://drive.google.com/file/d/1SchoolOnlyAbCdEfGhIjKl/view"
SNIFF = "https://drive.google.com/file/d/1OctetStreamAbCdEfGhIjK/preview"
GUARDIAN = "https://www.theguardian.com/us-news/story"
NYT = "https://www.nytimes.com/2026/08/28/us/politics/story.html"
VIDEO = "https://www.youtube.com/watch?v=abc"


@pytest.fixture
def internet(monkeypatch):
    pdf = make_pdf([(612, 792, [(72, 700, "Relative Velocity Inquiry Lab"), (72, 680, "Question 1: predict the angle.")])])

    def google(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "1SchoolOnly" in url:
            return httpx.Response(302, headers={"location": "https://accounts.google.com/ServiceLogin?continue=x"})
        if "accounts.google.com" in url:
            return httpx.Response(200, content=b"<html>Sign in</html>", headers={"content-type": "text/html"})
        if "1OctetStream" in url:
            return httpx.Response(200, content=pdf, headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, content=pdf, headers={
            "content-type": "application/pdf",
            "content-disposition": "attachment; filename=\"Lab handout.pdf\"; filename*=UTF-8''Relative%20Velocity%20Lab.pdf",
        })

    def site(request: httpx.Request) -> httpx.Response:
        if "nytimes" in request.url.host:
            return httpx.Response(403, text="Access denied")
        paragraph = "<p>" + "The court ruled on the case in a narrow decision that surprised observers. " * 8 + "</p>"
        html = f"<html><head><title>Supreme court rules</title></head><body><nav>Home News Sport</nav><article>{paragraph * 4}</article></body></html>"
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=httpx.MockTransport(google), follow_redirects=True))
    monkeypatch.setattr(web, "_client", lambda: httpx.Client(transport=httpx.MockTransport(site), follow_redirects=True))


def test_read_assignment_reports_every_link(internet):
    html = (
        f'<p>Read <a href="{DOC}">the notes</a>, <a href="{DRIVE}">the problems</a>, <a href="{GUARDIAN}">this</a>, '
        f'<a href="{NYT}">that</a> and watch <a href="{VIDEO}">the video</a>. Same notes again: <a href="{DOC}">notes</a>.</p>'
        f'<iframe title="Relative Velocity Inquiry Lab" src="{LAB}"></iframe>'
        f'<iframe title="scan.pdf" src="{SNIFF}"></iframe>'
    )
    docs = reader.read_assignment(Assignment(id=31, course_id=1, name="Lab", description_html=html))
    by_url = {d.url: d for d in docs}
    assert [d.url for d in docs[:2]] == [LAB, SNIFF]  # embedded docs first
    assert len(docs) == 7  # the repeated doc link is read once
    assert by_url[LAB].readable and by_url[LAB].name == "Relative Velocity Lab" and by_url[LAB].real_pages == 1
    assert "predict the angle" in by_url[LAB].text
    assert by_url[SNIFF].readable  # octet-stream sniffed as a PDF
    assert by_url[DRIVE].reason == "shared only inside the school" and not by_url[DRIVE].readable
    assert by_url[GUARDIAN].readable and by_url[GUARDIAN].name == "Supreme court rules"
    assert "Home News Sport" not in by_url[GUARDIAN].text and "narrow decision" in by_url[GUARDIAN].text
    assert by_url[NYT].reason.startswith("paywalled")
    assert by_url[VIDEO].kind == "video" and not by_url[VIDEO].readable


def test_model_files_send_big_docs_as_text(tmp_path):
    small = tmp_path / "small.pdf"
    small.write_bytes(make_pdf([spread(2)]))
    big = tmp_path / "big.pdf"
    big.write_bytes(make_pdf([spread(n) for n in range(10, 10 + 2 * 40, 2)]))  # 40 PDF pages
    docs = [reader.read_document(small), reader.read_document(big), reader.read_document(big, name="again")]
    files, sent = reader.model_files(docs[:2])
    assert [f.name for f in files] == ["small.pdf", "big.pdf"]
    object.__setattr__(reader, "MODEL_PAGE_BUDGET", 10)
    try:
        files, sent = reader.model_files(docs[:2])
    finally:
        object.__setattr__(reader, "MODEL_PAGE_BUDGET", 60)
    assert [f.name for f in files] == ["small.pdf", "big.text.txt"]
    assert "--- page 11 ---" in files[1].read_text(encoding="utf-8")


def test_attachments_give_the_card_its_sources(internet):
    from app import prep

    html = f'<p><a href="{DRIVE}">the problems</a></p><iframe title="Lab" src="{LAB}"></iframe>'
    files, sources = prep._attachments(Assignment(id=32, course_id=1, name="Lab", description_html=html))
    assert [f.suffix for f in files] == [".pdf"]
    assert [(s.name, s.read, s.reason) for s in sources] == [
        ("Relative Velocity Lab", True, ""),
        ("the problems", False, "shared only inside the school"),
    ]
