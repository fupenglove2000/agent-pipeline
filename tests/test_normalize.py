from agent_pipeline.normalize import normalise


def test_key_order_does_not_affect_result() -> None:
    a = {"source_id": "abc", "payload": {"b": 1, "a": 2}}
    b = {"payload": {"a": 2, "b": 1}, "source_id": "abc"}

    assert normalise(a) == normalise(b)


def test_whitespace_only_differences_do_not_affect_result() -> None:
    a = {"source_id": "abc", "text": "hello world"}
    b = {"source_id": " abc ", "text": "  hello world\n"}

    assert normalise(a) == normalise(b)


def test_reordered_list_elements_produce_a_different_result() -> None:
    a = {"steps": ["first", "second"]}
    b = {"steps": ["second", "first"]}

    assert normalise(a) != normalise(b)


def test_different_case_produces_a_different_result() -> None:
    a = {"text": "Hello"}
    b = {"text": "hello"}

    assert normalise(a) != normalise(b)


def test_different_value_type_produces_a_different_result() -> None:
    a = {"value": 1}
    b = {"value": "1"}

    assert normalise(a) != normalise(b)
