"""AI output parsing robustness (P0-3) + model-tiering config (G1)."""
import ai_resolve as a


def test_last_json_picks_final_balanced_object():
    text = ('I will search now.\n{"tool":"WebSearch"}\nFound it.\n'
            '{"website":"nutricost.com","confidence":"high","why":"matches"}')
    d = a._last_json(text)
    assert d["website"] == "nutricost.com"


def test_last_json_handles_nested_and_noise():
    text = 'noise {"a":{"b":1}} more {"website":"x.com","confidence":"high"} tail'
    d = a._last_json(text)
    assert d["website"] == "x.com"


def test_last_json_none_on_garbage():
    assert a._last_json("no json here") is None
    assert a._last_json("") is None
    assert a._last_json("{not valid}") is None


def test_envelope_result_extracts_result_field():
    env = '{"type":"result","result":"{\\"website\\":\\"y.com\\"}","total_cost_usd":0.01}'
    assert a._envelope_result(env) == '{"website":"y.com"}'
    # non-envelope passes through unchanged
    assert a._envelope_result("plain text") == "plain text"


def test_model_tiering_configured():
    # narrow skills should default to a cheap model, research to a mid model
    assert a.NARROW_MODEL  # non-empty
    assert a.RESEARCH_MODEL
    assert a.NARROW_MODEL != "opus" and a.RESEARCH_MODEL != "opus"
