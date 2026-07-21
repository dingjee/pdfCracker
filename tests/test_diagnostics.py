from pdfcracker.diagnostics import is_semantic_color


def test_semantic_color_detection() -> None:
    assert is_semantic_color((0.75, 0.0, 0.0)) is True
    assert is_semantic_color((0.25,)) is False
    assert is_semantic_color((0.2, 0.2, 0.2)) is False
