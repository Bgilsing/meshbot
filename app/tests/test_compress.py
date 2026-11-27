from app.compress import blen, clean, cut_bytes, fit, part_budget, split_text

NAME = "TestBot"
PTB = 140


def test_clean_flattens_markdown():
    t = clean("**Answer:**\n- one\n- two\n# Head\n`code`", NAME)
    assert "*" not in t and "#" not in t and "`" not in t and "\n" not in t
    assert "one; two" in t


def test_clean_strips_own_name_and_think():
    assert clean("<think>hmm</think>TestBot: hi there", NAME) == "hi there"


def test_cut_bytes_never_splits_utf8():
    s = "é" * 10  # 2 bytes each
    assert cut_bytes(s, 5) == "éé"


def test_single_part_fits():
    f = fit(["Water boils at 100C at sea level."], NAME, PTB, 3)
    assert f.parts == ["Water boils at 100C at sea level."]
    assert not f.overflow


def test_budget_accounts_for_name_and_markers():
    assert part_budget(PTB, NAME, 1) == PTB - len(NAME) - 2
    assert part_budget(PTB, NAME, 3) == PTB - len(NAME) - 2 - 4


def test_multi_part_every_packet_within_limit():
    text = " ".join(f"Sentence number {i} has some words in it." for i in range(12))
    f = fit([text], NAME, PTB, 3)
    assert 1 < len(f.parts) <= 3
    for i, p in enumerate(f.parts, 1):
        assert blen(f"{NAME}: {p}") <= PTB
        assert p.endswith(f" {i}/{len(f.parts)}")


def test_overflow_truncates_with_ellipsis():
    text = "word " * 400
    f = fit([text], NAME, PTB, 3)
    assert f.overflow and f.truncated and len(f.parts) == 3
    assert f.parts[-1].endswith("... 3/3")
    assert all(blen(f"{NAME}: {p}") <= PTB for p in f.parts)


def test_overflow_without_truncate_reports():
    f = fit(["word " * 400], NAME, PTB, 3, allow_truncate=False)
    assert f.overflow and not f.parts


def test_model_split_kept_when_valid():
    f = fit(["First part.", "Second part."], NAME, PTB, 3)
    assert f.parts == ["First part. 1/2", "Second part. 2/2"] and not f.resplit


def test_oversized_part_is_resplit():
    f = fit(["a" * 200], NAME, PTB, 3)
    assert len(f.parts) == 2


def test_multibyte_text_respects_bytes():
    text = "温度 " * 120
    f = fit([text], NAME, PTB, 3)
    assert all(blen(f"{NAME}: {p}") <= PTB for p in f.parts)


def test_long_token_hard_split():
    chunks = split_text("x" * 300, 100)
    assert all(blen(c) <= 100 for c in chunks) and "".join(chunks) == "x" * 300
