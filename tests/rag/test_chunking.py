"""문서 청크 분할 테스트."""

from app.rag import chunking
from app.rag.chunking import chunk_document, split_with_heading
from app.rag.store import to_pgvector


def split_text(text: str, **kwargs) -> list[str]:
    return split_with_heading(text, **kwargs)[0]


def test_paragraphs_are_grouped_under_size() -> None:
    text = "\n\n".join(f"문단 {i} " + "가" * 50 for i in range(10))

    chunks = split_text(text, size=200, overlap=20)

    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks).count("문단") == 10


def test_heading_starts_new_chunk_and_prefixes_long_paragraph_pieces() -> None:
    text = "#### □ 제출물 안내\n\n" + "나" * 450 + "\n\n#### □ 심사\n\n심사 내용"

    chunks = split_text(text, size=200, overlap=20)

    # 긴 문단 조각마다 제목이 붙어 무엇에 대한 내용인지 남는다.
    body = [c for c in chunks if "나" in c]
    assert len(body) >= 2
    assert all(c.startswith("#### □ 제출물 안내") for c in body)
    assert chunks[-1] == "#### □ 심사\n\n심사 내용"


def test_long_paragraph_pieces_overlap() -> None:
    paragraph = "".join(str(i % 10) for i in range(500))

    chunks = split_text(paragraph, size=200, overlap=50)

    # 이웃한 조각끼리 overlap 만큼 겹친다.
    assert chunks[0][-50:] == chunks[1][:50]
    assert "".join(c[:150] for c in chunks[:-1]) + chunks[-1] == paragraph


def test_markdown_document_has_no_page(tmp_path) -> None:
    path = tmp_path / "rules.md"
    path.write_text("# 규칙\n\n제출은 13시까지", encoding="utf-8")

    chunks = chunk_document(path)

    assert [(c.source, c.page, c.content) for c in chunks] == [
        ("rules.md", None, "# 규칙\n\n제출은 13시까지")
    ]


def test_to_pgvector_formats_list() -> None:
    assert to_pgvector([0.1, -0.25, 1.0]) == "[0.1,-0.25,1]"


def test_heading_carries_over_to_next_page(monkeypatch, tmp_path) -> None:
    pages = [(7, "본문\n\n#### □ 멘토링 안내"), (8, "멘토링은 토요일 종일\n\n- 8 -")]
    monkeypatch.setattr(chunking, "load_pages", lambda path: pages)

    chunks = chunk_document(tmp_path / "guide.pdf")

    # 제목만 있는 청크는 버리고, 다음 쪽 본문에 제목을 붙인다.
    assert [(c.page, c.content) for c in chunks] == [
        (7, "본문"),
        (8, "#### □ 멘토링 안내\n\n멘토링은 토요일 종일\n\n- 8 -"),
    ]


def test_page_footer_pattern_matches_only_footer_lines() -> None:
    text = "본문\n- 3 -\n- 항목 3 -\n"

    assert chunking.PAGE_FOOTER.sub("", text) == "본문\n\n- 항목 3 -\n"
