"""RAG 문서를 청크로 잘라 임베딩한 뒤 Supabase rag_chunks 테이블에 넣는다.

실행 전 scripts/schema.sql 의 rag_chunks 블록을 Supabase SQL Editor 에서 실행하고,
.env 에 GEMINI_API_KEY(또는 EMBEDDING_* 설정)를 넣는다.

    uv run python scripts/ingest.py "docs/참가자 가이드.pdf" [다른 파일 ...]

PDF, 마크다운(.md), 텍스트(.txt)를 받는다. 같은 파일을 다시 넣으면 기존 청크를 지우고
새로 넣는다. 넣은 뒤에는 서버를 다시 시작해야 분류기 문서 목록에 반영된다.
"""

import asyncio
import sys
from pathlib import Path

# `python scripts/ingest.py`로 실행해도 프로젝트 패키지를 찾는다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.core.db import create_pool
from app.rag import store
from app.rag.chunking import chunk_document
from app.rag.embeddings import build_embeddings


async def main(paths: list[Path]) -> None:
    embeddings = build_embeddings()
    print(
        f"embedding={settings.embedding_provider}:{settings.embedding_model} dim={settings.embedding_dim}"
    )

    pool = create_pool()
    await pool.open()
    try:
        for path in paths:
            chunks = chunk_document(path)
            vectors = await embeddings.aembed_documents([chunk.content for chunk in chunks])
            count = await store.replace_source(pool, path.name, chunks, vectors)
            longest = max(len(chunk.content) for chunk in chunks)
            print(f"{path.name}: {count} chunks (longest {longest} chars)")
    finally:
        await pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main([Path(arg) for arg in sys.argv[1:]]))
