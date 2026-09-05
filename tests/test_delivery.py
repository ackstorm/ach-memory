from memory.delivery import TOKENIZER_VERSION, DeliverySection, assemble_context, count_tokens


def test_delivery_tokenizer_is_versioned_and_allows_special_text():
    assert TOKENIZER_VERSION == "ach-delivery-o200k-v1"
    assert count_tokens("<|endoftext|>") > 0


def test_over_budget_entries_are_omitted_whole():
    payload = assemble_context([
        DeliverySection("a", "User · a", "x " * 600, 10),
        DeliverySection("b", "User · b", "kept", 10),
    ], global_max_tokens=100)
    assert "User · a" not in payload.text
    assert "User · b\nkept" in payload.text
    assert payload.omissions[0].reason == "model_output_over_budget"
