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


def test_a_model_body_uses_its_declared_budget_without_charging_the_heading():
    body = "x " * 10
    budget = count_tokens(body.strip())

    payload = assemble_context([
        DeliverySection("a", "A deliberately verbose model heading", body, budget),
    ])

    assert payload.headings == ["A deliberately verbose model heading"]
    assert payload.omissions == []


def test_global_budget_reports_the_entry_that_was_actually_omitted():
    oversized = DeliverySection("a", "A", "x " * 100, 1)
    kept = DeliverySection("b", "B", "kept", 10)
    displaced = DeliverySection("c", "C", "later", 10)
    budget = count_tokens("B\nkept")

    payload = assemble_context(
        [oversized, kept, displaced], global_max_tokens=budget
    )

    assert payload.text == "B\nkept"
    assert [(item.key, item.reason) for item in payload.omissions] == [
        ("a", "model_output_over_budget"),
        ("c", "global_budget"),
    ]
