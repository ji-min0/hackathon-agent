"""임베딩 나눠 보내기·재시도 테스트. 실제 API 는 부르지 않는다."""

import pytest

from app.rag import embeddings


class FakeEmbeddings:
    """정해 둔 횟수만큼 429 를 낸 뒤 텍스트 길이를 벡터로 돌려준다."""

    def __init__(self, fail_times: int = 0, error: str = "429 RESOURCE_EXHAUSTED") -> None:
        self.fail_times = fail_times
        self.error = error
        self.batches: list[int] = []

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError(self.error)
        self.batches.append(len(texts))
        return [[float(len(t))] for t in texts]


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(embeddings.asyncio, "sleep", fake_sleep)
    return slept


@pytest.mark.asyncio
async def test_splits_into_batches_and_keeps_order(waits) -> None:
    texts = ["x" * (i % 7 + 1) for i in range(200)]
    fake = FakeEmbeddings()

    vectors = await embeddings.embed_documents_in_batches(fake, texts)

    assert fake.batches == [90, 90, 20]
    assert vectors == [[float(len(t))] for t in texts]
    assert waits == []


@pytest.mark.asyncio
async def test_waits_and_retries_on_rate_limit(waits) -> None:
    fake = FakeEmbeddings(fail_times=2)

    vectors = await embeddings.embed_documents_in_batches(fake, ["a", "bb"])

    assert vectors == [[1.0], [2.0]]
    assert waits == [60, 60]


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(waits) -> None:
    fake = FakeEmbeddings(fail_times=1, error="401 invalid key")

    with pytest.raises(RuntimeError, match="401"):
        await embeddings.embed_documents_in_batches(fake, ["a"])
    assert waits == []


@pytest.mark.asyncio
async def test_gives_up_after_retries(waits) -> None:
    fake = FakeEmbeddings(fail_times=10)

    with pytest.raises(RuntimeError, match="429"):
        await embeddings.embed_documents_in_batches(fake, ["a"])
    assert len(waits) == embeddings.RATE_LIMIT_RETRIES
