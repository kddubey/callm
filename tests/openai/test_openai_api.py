"""
Unit tests `cappr.openai.api`. Currently pretty barebones.
"""
from __future__ import annotations

import openai
import pytest

from cappr.openai import api


def test_openai_method_retry():
    def openai_method(**kwargs):
        return kwargs["waddup"]

    assert api.openai_method_retry(openai_method, waddup="nada") == "nada"

    def openai_method():
        raise openai.error.ServiceUnavailableError

    with pytest.raises(openai.error.ServiceUnavailableError):
        api.openai_method_retry(openai_method, sleep_sec=0)


def test__openai_api_call_is_ok(monkeypatch):
    texts = ["cherry", "coke"]  # tokenized: [[331, 5515], [1030, 441]]
    model = "gpt-3.5-turbo-instruct"  # hard-coded so that tokenization is always same
    num_tokens_prompts = 4

    # Mimic the user seeing the prompt and entering y
    monkeypatch.setattr("builtins.input", lambda _: "y")
    max_tokens = 5  # number of completion tokens per prompt
    cost_per_1k_tokens_prompt = 1  # make it somewhat big to avoid numerical issues
    cost_per_1k_tokens_completion = 2
    input_prompt_observed = api._openai_api_call_is_ok(
        texts,
        model,
        max_tokens=max_tokens,
        cost_per_1k_tokens_prompt=cost_per_1k_tokens_prompt,
        cost_per_1k_tokens_completion=cost_per_1k_tokens_completion,
    )
    num_tokens_completions = len(texts) * max_tokens
    num_tokens_total = num_tokens_prompts + num_tokens_completions
    cost = round(
        (
            (num_tokens_prompts * cost_per_1k_tokens_prompt)
            + (num_tokens_completions * cost_per_1k_tokens_completion)
        )
        / 1_000,
        2,
    )
    input_prompt_expected = (
        f"This API call will cost about ${cost} (≤{num_tokens_total:_} tokens). "
        "Proceed? (y/n): "
    )
    assert input_prompt_observed == input_prompt_expected

    # Mimic the user seeing the prompt and entering n
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(api._UserCanceled):
        api._openai_api_call_is_ok(texts, model)


def test_gpt_chat_complete(monkeypatch):
    completion_expected = "heyteam howsitgoin"

    def mocked(openai_method, messages: list[dict[str, str]], **kwargs):
        # Technically, we should return a openai.openai_object.OpenAIObject
        # For now, just gonna return the minimum dict required
        return {"choices": [{"text": completion_expected}]}

    monkeypatch.setattr("cappr.openai.api.openai_method_retry", mocked)

    prompts = ["hey there", "hi", "hmm"]
    choices = api.gpt_chat_complete(prompts, model="o_o hi")
    completions = [choice["text"] for choice in choices]
    assert len(prompts) == len(completions)
    for completion_observed in completions:
        assert completion_observed == completion_expected
