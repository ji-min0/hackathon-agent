"""문서를 읽어 검색 단위(청크)로 자른다.

청크(chunk)는 임베딩해서 저장하는 문서 조각이다. 너무 길면 검색이 흐려지고,
너무 짧으면 답에 필요한 맥락이 잘린다. 문단 단위로 모아 CHUNK_SIZE 를 넘지 않게 자른다.
"""

import re
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

HEADING = re.compile(r"^#{1,6}\s+\S")
# PDF 쪽 번호 꼬리말("- 3 -"). 검색에 도움이 안 되고 청크만 쪼갠다.
PAGE_FOOTER = re.compile(r"^\s*-\s*\d+\s*-\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    source: str
    page: int | None
    content: str


def load_pages(path: Path) -> list[tuple[int | None, str]]:
    """문서를 (쪽 번호, 마크다운 텍스트) 목록으로 읽는다. 마크다운·텍스트는 쪽 번호가 없다."""
    if path.suffix.lower() == ".pdf":
        # PDF 파싱 라이브러리는 ingest 할 때만 필요해서 여기서 불러온다.
        from pymupdf import pymupdf4llm

        # 표는 마크다운 표로, 제목은 # 으로 바꿔 준다.
        pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        return [
            (page["metadata"]["page_number"], PAGE_FOOTER.sub("", page["text"])) for page in pages
        ]
    return [(None, path.read_text(encoding="utf-8"))]


def split_with_heading(
    text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP, heading: str = ""
) -> tuple[list[str], str]:
    """문단을 모아 size 이하의 청크로 자르고, 마지막으로 본 제목도 함께 돌려준다.

    각 청크 앞에는 그 문단이 속한 가장 가까운 제목을 붙인다. "제출 마감은 13시" 같은
    문단만 떼어 놓으면 무엇에 대한 내용인지 잃어버리기 때문이다. heading 으로 앞 쪽의
    마지막 제목을 넘겨받아, 쪽이 바뀌어도 제목이 이어지게 한다.
    """
    chunks: list[str] = []
    current = f"{heading}" if heading else ""

    def flush() -> None:
        nonlocal current
        # 제목만 있고 본문이 없는 청크는 버린다(본문은 다음 쪽에서 제목과 함께 들어간다).
        if current.strip() and current.strip() != heading:
            chunks.append(current.strip())
        current = ""

    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        if HEADING.match(paragraph):
            flush()
            heading = paragraph.splitlines()[0]
            current = paragraph
            continue

        if len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue

        flush()
        prefix = f"{heading}\n\n" if heading else ""
        # 한 문단이 size 보다 길면 겹치게 잘라서, 경계에 걸친 문장도 어느 한쪽에 온전히 남긴다.
        step = max(size - len(prefix) - overlap, 1)
        for start in range(0, len(paragraph), step):
            piece = paragraph[start : start + step + overlap]
            current = prefix + piece
            if start + step < len(paragraph):
                flush()

    flush()
    return chunks, heading


def chunk_document(path: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = ""
    for page, text in load_pages(path):
        contents, heading = split_with_heading(text, heading=heading)
        chunks += [Chunk(source=path.name, page=page, content=c) for c in contents]
    return chunks
