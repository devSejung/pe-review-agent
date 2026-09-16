from pe_review_agent.review.chunking import chunk_diff


def test_chunk_diff_prefers_file_boundaries_and_preserves_all_text() -> None:
    first = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n" + "+a\n" * 10
    second = "diff --git a/b.c b/b.c\n--- a/b.c\n+++ b/b.c\n" + "+b\n" * 10
    diff = first + second

    chunks = chunk_diff(diff, max_chars=len(first) + 5)

    assert "".join(chunk.text for chunk in chunks) == diff
    assert chunks[0].paths == ("a.c",)
    assert chunks[1].paths == ("b.c",)


def test_chunk_diff_splits_oversized_single_file_without_dropping_text() -> None:
    diff = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n" + "+long_line();\n" * 100

    chunks = chunk_diff(diff, max_chars=120)

    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == diff
    assert all(len(chunk.text) <= 120 for chunk in chunks)
    assert all(chunk.paths == ("a.c",) for chunk in chunks)
