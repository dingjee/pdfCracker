from pdfcracker.cache import cleanup_source_cache, load_pdftext_pages


def test_pdftext_cache_hits_and_cleans_only_source_entry(tmp_path, monkeypatch) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"test-pdf-bytes")
    cache_root = tmp_path / "cache"
    calls = 0

    def fake_dictionary_output(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [
            {
                "page": 0,
                "width": 10,
                "height": 10,
                "blocks": [],
                "parser_private_reference": object(),
            }
        ]

    monkeypatch.setattr("pdfcracker.cache.dictionary_output", fake_dictionary_output)
    first, first_metadata = load_pdftext_pages(source, "a" * 64, cache_root)
    second, second_metadata = load_pdftext_pages(source, "a" * 64, cache_root)

    assert first == second
    assert "parser_private_reference" not in first[0]
    assert calls == 1
    assert first_metadata["hit"] is False
    assert second_metadata["hit"] is True
    assert cleanup_source_cache(cache_root, "a" * 64) is True
    assert not cache_root.exists()
