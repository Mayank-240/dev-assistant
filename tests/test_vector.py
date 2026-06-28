from ai_dev_assistant.memory.embeddings import HashingEmbedder
from ai_dev_assistant.memory.store import MemoryStore


def test_cosine_ranks_identical_text_highest():
    store = MemoryStore.in_memory()
    vs = store.vectors
    vs.add("ns", "a", "graph databases store nodes and edges")
    vs.add("ns", "b", "the weather today is sunny and warm")

    results = vs.search("ns", "graph databases store nodes and edges", top_k=2)
    assert results[0][0] == "a"
    assert results[0][1] > 0.99  # identical text -> cosine ~1.0
    assert results[0][1] >= results[1][1]


def test_hashing_embedder_is_deterministic():
    emb = HashingEmbedder(dim=128)
    v1 = emb.embed(["hello world"])[0]
    v2 = emb.embed(["hello world"])[0]
    assert v1 == v2
    assert len(v1) == 128
