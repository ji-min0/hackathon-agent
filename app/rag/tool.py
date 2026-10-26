"""문서 검색 툴. RAG 에이전트가 질문과 뜻이 가까운 청크를 찾을 때 부른다."""

from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool, tool

from app.core.config import settings
from app.rag import store


def format_passages(rows: list[dict]) -> str:
    """검색된 청크를 모델이 출처를 인용할 수 있는 형태로 이어 붙인다."""
    if not rows:
        return "No matching passages found in the documents."
    blocks = []
    for row in rows:
        page = f" p.{row['page']}" if row["page"] is not None else ""
        blocks.append(f"[{row['source']}{page}] (score {row['score']:.2f})\n{row['content']}")
    return "\n\n---\n\n".join(blocks)


def build_document_search_tool(pool, embeddings: Embeddings) -> BaseTool:
    """등록된 문서에서 질문과 뜻이 가까운 청크를 찾는 툴."""

    async def search_documents(query: str) -> str:
        """Searches the registered internal documents and returns the most relevant passages
        with their document name and page."""
        vector = await embeddings.aembed_query(query)
        return format_passages(await store.search(pool, vector, settings.rag_top_k))

    return tool(search_documents)
