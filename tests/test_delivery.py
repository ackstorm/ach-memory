from memory.delivery import TOKENIZER_VERSION, DeliverySection, assemble_context, count_tokens


def test_delivery_tokenizer_is_versioned_and_allows_special_text():
    assert TOKENIZER_VERSION == "ach-delivery-o200k-v1"
    assert count_tokens("<|endoftext|>") > 0


def test_an_over_budget_entry_is_delivered_and_reported_not_dropped():
    """A budget is what the model was asked for, not what it wrote.

    Dropping the whole section threw away the only copy of that context the
    session was going to get: measured 2026-09-07, a 574-token user-context
    vanished against a 512-token budget and standing context arrived empty.
    """
    payload = assemble_context([
        DeliverySection("a", "User · a", "x " * 600, 10),
        DeliverySection("b", "User · b", "kept", 10),
    ], global_max_tokens=10_000)

    assert "User · a" in payload.text
    assert "User · b\nkept" in payload.text
    assert payload.omissions == []
    assert [(item.key, item.max_tokens) for item in payload.overages] == [("a", 10)]
    assert payload.overages[0].token_count > 10


def test_a_model_body_uses_its_declared_budget_without_charging_the_heading():
    body = "x " * 10
    budget = count_tokens(body.strip())

    payload = assemble_context([
        DeliverySection("a", "A deliberately verbose model heading", body, budget),
    ])

    assert payload.headings == ["A deliberately verbose model heading"]
    assert payload.omissions == []


def test_global_budget_omits_from_the_end_and_reports_what_it_dropped():
    kept = DeliverySection("a", "A", "kept", 10)
    displaced = DeliverySection("b", "B", "later", 10)
    budget = count_tokens("A\nkept")

    payload = assemble_context([kept, displaced], global_max_tokens=budget)

    assert payload.text == "A\nkept"
    assert [(item.key, item.reason) for item in payload.omissions] == [
        ("b", "global_budget")
    ]


def test_the_last_entry_survives_the_global_budget_rather_than_nothing():
    """The hard ceiling never empties the payload.

    Popping from the end until the total fits used to be able to pop the
    only remaining section too, so one oversized model turned the whole
    standing context into an empty string."""
    oversized = DeliverySection("a", "A", "x " * 100, 1)
    displaced = DeliverySection("b", "B", "later", 10)

    payload = assemble_context([oversized, displaced], global_max_tokens=5)

    assert payload.text.startswith("A\n")
    assert payload.total_tokens > 5
    assert [(item.key, item.reason) for item in payload.omissions] == [
        ("b", "global_budget")
    ]
    assert [item.key for item in payload.overages] == ["a"]
