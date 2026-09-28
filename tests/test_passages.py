from conftest import run

from orbwise.tools.passages import rank_passages, select_passages, split_passages


def test_split_passages_overlap_and_coverage():
    text = ("Satz eins ist hier. " * 300).strip()
    parts = split_passages(text, 500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 10
    assert parts[-1].endswith("hier.")


def test_rank_by_keywords_without_embeddings():
    passages = ["Das Wetter ist schön.", "Die Kündigungsfrist beträgt drei Monate.", "Sonstiges."]
    assert run(rank_passages("Wie lange ist die Kündigungsfrist?", passages))[0] == 1


def test_select_respects_budget_keeps_text_order():
    passages = ["a" * 100, "b" * 100, "c" * 100]
    assert select_passages(passages, [2, 0, 1], 250) == [0, 2]
    assert select_passages(passages, [1, 0], 10) == [1]
