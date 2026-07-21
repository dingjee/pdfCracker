from pdfcracker.vision import _provider_for_model, score_audit


def test_score_audit() -> None:
    gold = {
        "page_type": "body",
        "features": {"heading": True, "red_text": False},
    }
    audit = {
        "page_type": "body",
        "features": {"heading": True, "red_text": True},
    }
    result = score_audit(audit, gold)
    assert result["page_type_match"] is True
    assert result["feature_accuracy"] == 0.5


def test_provider_routing_uses_canonical_slugs() -> None:
    assert _provider_for_model("google/gemini-3.1-flash-lite") == "google-ai-studio"
    assert _provider_for_model("openai/gpt-5.4-mini") == "openai"
