import pytest

from freya.gpu import __main__ as gpu_cli
from freya.gpu import model_fit
from freya.gpu.model_fit import (
    GIB,
    ON_CARD,
    SPLIT,
    TOO_LARGE,
    Machine,
    ModelFacts,
    facts_from_figures,
    facts_from_ollama,
    kv_cache_bytes,
    measure,
    plan,
)


# This workstation as recorded: a 24 GB card and 64 GB of memory.
WORKSTATION = Machine(vram_total_gb=24.0, ram_total_gb=64.0)


def dense(name="dense-27b", size_gb=16.0, layers=64):
    return ModelFacts(
        name=name,
        file_bytes=int(size_gb * GIB),
        layers=layers,
        kv_heads=4,
        key_length=128,
        value_length=128,
        total_params=27_000_000_000,
    )


def moe(name="moe-120b", size_gb=61.0):
    # Shaped like a large open mixture-of-experts model: 128 experts,
    # four used per token, experts most of the weights.
    return ModelFacts(
        name=name,
        file_bytes=int(size_gb * GIB),
        layers=36,
        kv_heads=8,
        key_length=64,
        value_length=64,
        total_params=117_000_000_000,
        expert_count=128,
        experts_used=4,
        expert_params=110_000_000_000,
        cached_layers=18,
    )


def test_a_27b_model_fits_on_the_card():
    result = plan(dense(), WORKSTATION, 16384)

    assert result.verdict == ON_CARD
    assert result.in_ram_gb == 0
    assert result.card_share_percent == 100


def test_a_large_mixture_of_experts_model_splits_with_only_experts_leaving():
    result = plan(moe(), WORKSTATION, 16384)

    assert result.verdict == SPLIT
    assert result.in_ram_gb > 0
    assert any("Only experts" in note for note in result.notes)


def test_experts_in_memory_are_much_faster_than_a_dense_split_of_the_same_size():
    big_dense = dense("dense-120b", size_gb=61.0, layers=88)

    moe_speed = plan(moe(), WORKSTATION, 16384).tokens_per_second_ceiling
    dense_speed = plan(big_dense, WORKSTATION, 16384).tokens_per_second_ceiling

    assert moe_speed > 5 * dense_speed


def test_a_model_larger_than_card_and_memory_is_refused():
    result = plan(moe("huge", size_gb=120.0), WORKSTATION, 16384)

    assert result.verdict == TOO_LARGE
    assert any("more than the card" in note for note in result.notes)


def test_reserves_come_off_both_budgets():
    machine = Machine(
        vram_total_gb=24.0, ram_total_gb=64.0, vram_reserve_gb=4, ram_reserve_gb=20
    )

    assert machine.vram_budget_gb == 20
    assert machine.ram_budget_gb == 44


def test_quantized_cache_is_smaller_and_scales_with_context():
    facts = dense()

    f16 = kv_cache_bytes(facts, 16384, "f16")
    q8 = kv_cache_bytes(facts, 16384, "q8_0")

    assert q8 < f16
    assert kv_cache_bytes(facts, 32768, "f16") == 2 * f16


def test_only_full_length_layers_are_cached():
    facts = moe()
    whole = ModelFacts(**{**facts.__dict__, "cached_layers": 0})

    assert kv_cache_bytes(facts, 8192) * 2 == kv_cache_bytes(whole, 8192)


def test_a_model_too_big_to_stay_cached_warns_about_image_reloads():
    result = plan(moe(size_gb=70.0), WORKSTATION, 16384)

    assert any("every reload" in note for note in result.notes)


def test_figures_from_a_download_page_mark_a_mixture_of_experts_model():
    facts = facts_from_figures("candidate", 61, total_b=117, active_b=5.1)

    assert facts.mixture_of_experts
    assert 0 < model_fit.expert_read_ratio(facts) < 0.1
    assert any("Give --layers" in note for note in facts.notes)


def test_figures_without_active_count_are_dense():
    facts = facts_from_figures("dense", 40, total_b=70)

    assert not facts.mixture_of_experts


class FakeClient:
    def __init__(self):
        self.calls = []

    def list(self):
        return {"models": [
            {"model": "big-moe:latest", "size": int(61 * GIB)},
            {"model": "nomic-embed-text:latest", "size": 274_000_000},
        ]}

    def show(self, model):
        return {"model_info": {
            "general.architecture": "testmoe",
            "general.parameter_count": 117_000_000_000,
            "testmoe.block_count": 36,
            "testmoe.embedding_length": 2880,
            "testmoe.attention.head_count": 64,
            "testmoe.attention.head_count_kv": 8,
            "testmoe.attention.key_length": 64,
            "testmoe.attention.value_length": 64,
            "testmoe.attention.sliding_window": 128,
            "testmoe.expert_count": 128,
            "testmoe.expert_used_count": 4,
            "testmoe.expert_feed_forward_length": 2880,
        }}

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "load_duration": 30_000_000_000,
            "eval_count": 300,
            "eval_duration": 15_000_000_000,
            "prompt_eval_count": 40,
            "prompt_eval_duration": 200_000_000,
        }

    def ps(self):
        return {"models": [
            {"model": "big-moe:latest", "size": int(64 * GIB),
             "size_vram": int(21 * GIB)},
        ]}


def test_facts_are_read_from_ollamas_description():
    facts = facts_from_ollama(FakeClient(), "big-moe")

    assert facts.layers == 36
    assert facts.kv_heads == 8
    assert facts.expert_count == 128
    assert facts.experts_used == 4
    assert facts.cached_layers == 18
    assert facts.mixture_of_experts
    assert 0 < facts.active_params < facts.total_params


def test_a_model_that_is_not_installed_is_reported():
    with pytest.raises(LookupError):
        facts_from_ollama(FakeClient(), "missing-model")


def test_measure_reports_speed_and_where_the_model_went():
    client = FakeClient()

    result = measure(client, "big-moe", 16384)

    assert result["tokens_per_second"] == 20.0
    assert result["load_seconds"] == 30.0
    assert result["card_share_percent"] == 33
    # Freya's context size, and no layer count forced on the runtime,
    # so it is free to choose an expert-aware split.
    options = client.calls[0]["options"]
    assert options == {"num_ctx": 16384}


def test_the_command_checks_a_model_from_figures(monkeypatch, capsys):
    monkeypatch.setattr(
        model_fit, "this_machine", lambda **overrides: WORKSTATION
    )

    code = gpu_cli.main(
        ["fit", "--name", "candidate", "--size-gb", "61",
         "--total-b", "117", "--active-b", "5.1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "candidate" in out
    assert SPLIT in out


def test_the_command_skips_embedding_models(monkeypatch, capsys):
    monkeypatch.setattr(model_fit, "this_machine", lambda **overrides: WORKSTATION)
    monkeypatch.setattr(gpu_cli, "_client", lambda: FakeClient())

    gpu_cli.main(["fit"])

    out = capsys.readouterr().out
    assert "big-moe" in out
    assert "nomic" not in out
