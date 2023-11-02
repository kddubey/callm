"""
Unit and integration tests for `cappr.huggingface.classify`. Works by checking that its
functions' outputs are numerically close to those from
`cappr.huggingface.classify_no_cache`.
"""
from __future__ import annotations
import os
import sys
from typing import Sequence

import pytest

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedTokenizerBase
from transformers.modeling_outputs import CausalLMOutput
import huggingface_hub as hf_hub

from cappr import Example
from cappr.huggingface import classify, classify_no_cache
from cappr import huggingface as hf
from cappr.huggingface._utils import BatchEncoding, ModelForCausalLM

# sys hack to import from parent. If someone has a cleaner solution, lmk
sys.path.insert(1, os.path.join(sys.path[0], ".."))
from _base import BaseTestPromptsCompletions, BaseTestExamples
import _test_form
import _test_content
from _protocol import classify_module


########################################################################################
###################################### Fixtures ########################################
########################################################################################


@pytest.fixture(
    scope="module",
    params=[
        "hf-internal-testing/tiny-random-GPT2LMHeadModel",
        "Maykeye/TinyLLama-v0",
        "hf-internal-testing/tiny-random-GPTJForCausalLM",
        "hf-internal-testing/tiny-random-GPTNeoXForCausalLM",
        "hf-internal-testing/tiny-random-MistralForCausalLM",
    ],
)
def model_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="module")
def model(model_name: str) -> ModelForCausalLM:
    model: ModelForCausalLM = AutoModelForCausalLM.from_pretrained(model_name)
    # Set attributes to values that would break CAPPr, if not for the context managers
    model.train()  # LMs w/ dropout (GPT-2) will cause mismatched logits b/c random
    model.config.return_dict = False  # out.logits fails (not for transformers>=4.31)
    setattr(model.config, "use_cache", False)  # out.past_key_values fails
    return model


@pytest.fixture(scope="module")
def tokenizer(model_name: str) -> PreTrainedTokenizerBase:
    # hf-internal-testing/tiny-random-MistralForCausalLM's tokenizer_config.json has a
    # field, tokenizer_file, which is hard-coded to some specific machine
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
    except:
        # tokenizer_file not found. Find it locally
        local_path = hf_hub.try_to_load_from_cache(model_name, "tokenizer.json")
        tokenizer = AutoTokenizer.from_pretrained(model_name, tokenizer_file=local_path)
    # Set attributes to values that would break CAPPr, if not for the context managers
    tokenizer.padding_side = "left"  # mismatched logits content b/c of position IDs
    if hasattr(tokenizer, "add_eos_token"):
        setattr(tokenizer, "add_eos_token", True)  # mismatched logits shape
    return tokenizer


@pytest.fixture(scope="module")
def model_and_tokenizer(
    model, tokenizer
) -> tuple[ModelForCausalLM, PreTrainedTokenizerBase]:
    return model, tokenizer


@pytest.fixture(scope="module")
def atol() -> float:
    # Reading through some transformers tests, it looks like 1e-3 is considered
    # close-enough for hidden states. See, e.g.,
    # https://github.com/huggingface/transformers/blob/897a826d830e8b1e03eb482b165b5d88a7a08d5f/tests/models/gpt2/test_modeling_gpt2.py#L252
    return 1e-4


########################################################################################
#################################### One-off tests #####################################
########################################################################################


def test_set_up_model_and_tokenizer(
    model: ModelForCausalLM, tokenizer: PreTrainedTokenizerBase
):
    """
    Tests that the context manager doesn't change any attributes of the model or
    tokenizer after exiting the context.
    """

    # Grab old attribute values.
    model_attribute_to_old_value = {
        "training": model.training,
        **{i: module.training for i, module in enumerate(model.children())},
    }
    model_config_attribute_to_old_value = {
        "return_dict": model.config.return_dict,
        "use_cache": getattr(model.config, "use_cache", None),
    }
    tokenizer_attributes = [
        "pad_token_id",
        "padding_side",
        "add_eos_token",
        "pad_token",
        "special_tokens_map",
    ]
    tokenizer_attribute_to_old_value = {
        attribute: getattr(tokenizer, attribute, None)
        # None is for tokenizers which don't have an add_eos_token attribute
        for attribute in tokenizer_attributes
    }

    # Enter the context
    assert torch.is_grad_enabled()
    with hf._utils.set_up_model_and_tokenizer(model, tokenizer):
        # TODO: add manual checks for correct attribute values!
        assert not torch.is_grad_enabled()
    assert torch.is_grad_enabled()

    # Exit the context. No attributes should have changed.
    assert model.training == model_attribute_to_old_value["training"]
    for i, module in enumerate(model.children()):
        assert module.training == model_attribute_to_old_value[i]

    for attribute, old_value in model_config_attribute_to_old_value.items():
        assert getattr(model.config, attribute, None) == old_value

    for attribute, old_value in tokenizer_attribute_to_old_value.items():
        assert getattr(tokenizer, attribute, None) == old_value


@pytest.mark.parametrize("texts", (["a b c", "d e", "f g h i"], ["slow reverb"]))
@pytest.mark.parametrize("batch_size", (1, 2))
def test__batched_model_call(texts, model, tokenizer, batch_size, atol):
    with hf._utils.set_up_tokenizer(tokenizer):
        encodings: BatchEncoding = tokenizer(texts, return_tensors="pt", padding=True)
    with hf._utils.set_up_model(model):
        out_correct: CausalLMOutput = model(**encodings)
    out_batched: CausalLMOutput = hf._utils._batched_model_call(
        batch_size=batch_size, model=model, **encodings
    )
    assert torch.allclose(out_correct.logits, out_batched.logits, atol=atol)


@pytest.mark.parametrize("module", (classify, classify_no_cache))
@pytest.mark.parametrize(
    "texts",
    (
        "lone string input",
        ["a b", "c d e"],
        ["a fistful", "of tokens", "for a few", "tokens more"],
    ),
)
@pytest.mark.parametrize("batch_size", (1, 2))
def test_token_logprobs(
    module: classify_module, texts, model_and_tokenizer, batch_size, end_of_prompt=" "
):
    """
    Tests that the model's token log probabilities are correct by testing against an
    unbatched and carefully, manually indexed result.
    """
    log_probs_texts_observed = module.token_logprobs(
        texts, model_and_tokenizer, end_of_prompt=end_of_prompt, batch_size=batch_size
    )
    # Gather un-batched un-sliced log probs for the expected result
    is_str = isinstance(texts, str)
    texts = [texts] if is_str else texts
    # bleh
    if not hf._utils.does_tokenizer_prepend_space_to_first_token(
        model_and_tokenizer[1]
    ):
        end_of_prompt = ""
    log_probs_texts_from_unbatched = []
    input_ids_from_unbatched = []
    for text in texts:
        _logits, _encoding = hf._utils.logits_texts(
            [end_of_prompt + text], model_and_tokenizer
        )
        # grab first index b/c we only gave it 1 text
        log_probs_texts_from_unbatched.append(_logits[0].log_softmax(dim=1))
        input_ids_from_unbatched.append(_encoding["input_ids"][0])

    log_probs_texts_observed = (
        [log_probs_texts_observed] if is_str else log_probs_texts_observed
    )
    _test_content.token_logprobs(
        log_probs_texts_observed,
        log_probs_texts_from_unbatched,
        input_ids_from_unbatched,
    )


########################################################################################
################################## Test cache context ##################################
########################################################################################


def test_cache_logits(model_and_tokenizer, atol):
    delim = " "
    if not hf._utils.does_tokenizer_prepend_space_to_first_token(
        model_and_tokenizer[1]
    ):
        # for SentencePiece tokenizers like Llama's
        delim = ""

    logits = lambda *args, **kwargs: hf._utils.logits_texts(*args, **kwargs)[0]
    """
    Returns next-token logits for each token in an inputted text.
    """

    with classify.cache(model_and_tokenizer, "a") as cached_a:
        with classify.cache(cached_a, delim + "b c") as cached_a_b_c:
            with classify.cache(cached_a_b_c, delim + "d") as cached_a_b_c_d:
                logits1 = logits([delim + "e f"], cached_a_b_c_d)
                logits2 = logits([delim + "x"], cached_a_b_c_d)
            logits3 = logits([delim + "1 2 3"], cached_a_b_c)
        logits4 = logits([delim + "b c d"], cached_a)

    logits_correct = lambda texts, **kwargs: logits(
        texts, model_and_tokenizer, drop_bos_token=False
    )

    assert torch.allclose(logits1, logits_correct(["a b c d e f"]), atol=atol)
    assert torch.allclose(logits2, logits_correct(["a b c d x"]), atol=atol)
    assert torch.allclose(logits3, logits_correct(["a b c 1 2 3"]), atol=atol)
    assert torch.allclose(logits4, logits_correct(["a b c d"]), atol=atol)

    # Test clear_cache_on_exit
    with pytest.raises(AttributeError, match="This model is no longer usable."):
        cached_a[0](input_ids=None, attention_mask=None)

    with classify.cache(
        model_and_tokenizer, "a", clear_cache_on_exit=False
    ) as cached_a:
        logits(["whatever"], cached_a)
    assert hasattr(cached_a[0], "_cappr_past")


# TODO: share w/ tests/test_llama_cpp_classify
@pytest.mark.parametrize(
    "completions",
    (
        ["e f", "f g h i j"],  # multiple tokens
        ["e", "f"],  # single tokens
    ),
)
@pytest.mark.parametrize("batch_size", (1, 2, 10))
class TestCache:
    def test_cache(self, model_and_tokenizer, completions, batch_size):
        prompt_prefix = "a b c"
        prompts = ["d", "d e"]

        with classify.cache(model_and_tokenizer, prompt_prefix) as cached:
            log_probs_completions = classify.log_probs_conditional(
                prompts, completions, cached, batch_size=batch_size
            )
        _test_form._test_log_probs_conditional(
            log_probs_completions,
            expected_len=len(prompts),
            num_completions_per_prompt=[len(completions)] * len(prompts),
        )

        prompts_full = [prompt_prefix + " " + prompt for prompt in prompts]
        log_probs_completions_wo_cache = classify_no_cache.log_probs_conditional(
            prompts_full, completions, model_and_tokenizer
        )
        _test_content._test_log_probs_conditional(
            log_probs_completions, log_probs_completions_wo_cache, is_single_input=False
        )

    def test_cache_examples(self, model_and_tokenizer, completions, batch_size):
        prompt_prefix = "a b c"
        _prompts = ["d", "d e"]
        examples = [Example(prompt, completions) for prompt in _prompts]

        with classify.cache(model_and_tokenizer, prompt_prefix) as cached:
            log_probs_completions = classify.log_probs_conditional_examples(
                examples, cached, batch_size=batch_size
            )
        _test_form._test_log_probs_conditional(
            log_probs_completions,
            expected_len=len(examples),
            num_completions_per_prompt=[
                len(example.completions) for example in examples
            ],
        )

        examples_full = [
            Example(prompt_prefix + " " + example.prompt, example.completions)
            for example in examples
        ]
        log_probs_completions_wo_cache = (
            classify_no_cache.log_probs_conditional_examples(
                examples_full, model_and_tokenizer
            )
        )
        _test_content._test_log_probs_conditional(
            log_probs_completions, log_probs_completions_wo_cache, is_single_input=False
        )


########################################################################################
################################### Helpers for tests ##################################
########################################################################################


def _test_encodings(
    logits_slow: torch.Tensor,
    encodings_slow: BatchEncoding,
    logits_fast: torch.Tensor,
    encodings_fast: BatchEncoding,
):
    """
    Tests that all objects have the expected shape, and that the encodings `offsets` are
    identical.
    """
    if logits_slow.shape[0] > logits_fast.shape[0] and logits_fast.shape[1] == 1:
        # Single-token optimization: this test doesn't apply b/c the optimization
        # doesn't repeat any data, unlike what's done in the slow/no-cache module
        return

    def _test_shapes(logits: torch.Tensor, encodings: BatchEncoding):
        assert encodings["input_ids"].shape == logits.shape[:2]  # 3rd dim is vocab
        assert encodings["input_ids"].shape == encodings["attention_mask"].shape
        assert encodings["input_ids"].shape[0] == encodings["offsets"].shape[0]

    _test_shapes(logits_slow, encodings_slow)
    _test_shapes(logits_fast, encodings_fast)

    # Test offsets. These should be exactly the same b/c they're the number of
    # of non-pad tokens in each prompt
    assert torch.equal(encodings_slow["offsets"], encodings_fast["offsets"])


def _test_logits(
    logits_slow: torch.Tensor,
    encodings_slow: BatchEncoding,
    logits_fast: torch.Tensor,
    encodings_fast: BatchEncoding,
    atol,
):
    """
    Tests that logits have identical shape, and that their non-pad token logits are
    numerically close.
    """
    if logits_slow.shape[0] > logits_fast.shape[0] and logits_fast.shape[1] == 1:
        # Single-token optimization: we only need to compare the last nonpad token's
        # logits for each prompt.
        num_completions = int(logits_slow.shape[0] / logits_fast.shape[0])
        logits_fast = logits_fast.repeat_interleave(num_completions, dim=0)
        last_nonpad_token_idxs = (encodings_slow["offsets"] - 1)[:, None, None]
        logits_slow_last_nonpad_token = logits_slow.take_along_dim(
            last_nonpad_token_idxs, dim=1
        )
        assert (
            logits_fast.shape == logits_slow_last_nonpad_token.shape
        )  # allclose doesn't check this
        assert torch.allclose(logits_fast, logits_slow_last_nonpad_token, atol=atol)
        return

    # Middle dimension (for the # of tokens) is different b/c logits_slow includes
    # prompt and completion tokens, while logits_fast only includes completion tokens.
    assert logits_slow.shape[2] == logits_fast.shape[2]  # vocab size

    # Test logits at every *non-pad* token
    completion_token_idxs = [
        list(range(num_completion_tokens))
        for num_completion_tokens in encodings_fast["attention_mask"].sum(dim=1)
    ]
    for text_idx in range(logits_slow.shape[0]):
        offset = encodings_fast["offsets"][text_idx].item() - 1
        # number of non-pad prompt tokens - 1 (!) b/c in the fast version we
        # included the last non-pad prompt token
        for completion_token_idx in completion_token_idxs[text_idx]:
            assert torch.allclose(
                logits_fast[text_idx, completion_token_idx],
                logits_slow[text_idx, offset + completion_token_idx],
                atol=atol,
            )


########################################################################################
####################################### Tests ##########################################
########################################################################################


class Modules:
    @property
    def module_correct(self):
        return classify_no_cache

    @property
    def modules_to_test(self):
        return (classify,)


class TestPromptsCompletions(Modules, BaseTestPromptsCompletions):
    def test__logits_completions_given_prompts(
        self, model, tokenizer, prompts, completions, atol
    ):
        # for this function, prompts can't be a single string
        if isinstance(prompts, str):
            return
        slow_out = classify_no_cache._logits_completions_given_prompts(
            model, tokenizer, prompts, completions
        )
        fast_out = classify._logits_completions_given_prompts(
            model, tokenizer, prompts, completions
        )
        _test_encodings(*slow_out, *fast_out)
        _test_logits(*slow_out, *fast_out, atol)

    @pytest.mark.parametrize("batch_size", (1, 2))
    @pytest.mark.parametrize("batch_size_completions", (None, 1))
    def test_log_probs_conditional(
        self,
        prompts,
        completions,
        model_and_tokenizer,
        batch_size,
        batch_size_completions,
    ):
        super().test_log_probs_conditional(
            prompts,
            completions,
            model_and_tokenizer,
            batch_size=batch_size,
            batch_size_completions=batch_size_completions,
        )

    def test_predict_proba(
        self,
        prompts,
        completions,
        model_and_tokenizer,
        _use_prior,
        discount_completions,
        normalize,
    ):
        super().test_predict_proba(
            prompts,
            completions,
            model_and_tokenizer,
            _use_prior=_use_prior,
            discount_completions=discount_completions,
            normalize=normalize,
        )

    def test_predict(self, prompts, completions, model_and_tokenizer):
        super().test_predict(prompts, completions, model_and_tokenizer)


class TestExamples(Modules, BaseTestExamples):
    def test__logits_completions_given_prompts_examples(
        self, model, tokenizer, examples, atol
    ):
        # for this helper function, examples can't be an Example
        if isinstance(examples, Example):
            return
        slow_out = classify_no_cache._logits_completions_given_prompts_examples(
            model, tokenizer, examples
        )
        fast_out = classify._logits_completions_given_prompts_examples(
            model, tokenizer, examples
        )
        _test_encodings(*slow_out, *fast_out)
        _test_logits(*slow_out, *fast_out, atol)

    @pytest.mark.parametrize("batch_size", (1, 2))
    def test_log_probs_conditional_examples(
        self, examples: Example | Sequence[Example], model_and_tokenizer, batch_size
    ):
        super().test_log_probs_conditional_examples(
            examples, model_and_tokenizer, batch_size=batch_size
        )

    def test_predict_proba_examples(
        self, examples: Example | Sequence[Example], model_and_tokenizer
    ):
        super().test_predict_proba_examples(examples, model_and_tokenizer)

    def test_predict_examples(
        self, examples: Example | Sequence[Example], model_and_tokenizer
    ):
        super().test_predict_examples(examples, model_and_tokenizer)
