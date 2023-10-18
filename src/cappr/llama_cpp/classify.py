"""
Perform prompt-completion classification using a model which can be loaded via
``llama_cpp.Llama``.

You probably just want the :func:`predict` or :func:`predict_examples` functions :-)

.. note:: When instantiating your Llama, set ``logits_all=True``.

The examples below use a 6 MB model to quickly demonstrate functionality. To download
it, first install ``huggingface-hub`` if you don't have it already::

    pip install huggingface-hub

And then download `the model
<https://huggingface.co/aladar/TinyLLama-v0-GGUF/blob/main/TinyLLama-v0.Q8_0.gguf>`_ (to
your current working directory)::

    huggingface-cli download \\
    aladar/TinyLLama-v0-GGUF \\
    TinyLLama-v0.Q8_0.gguf \\
    --local-dir . \\
    --local-dir-use-symlinks False
"""
# TODO: may need to support end_of_prompt for GPT/BPE models
from __future__ import annotations
from typing import Sequence

from llama_cpp import Llama
import numpy as np
import numpy.typing as npt
from tqdm.auto import tqdm

from cappr.utils import _batch, _check, classify
from cappr import Example
from cappr.llama_cpp._utils import log_softmax, logits_to_log_probs


def _check_model(model: Llama):
    """
    Raises a `TypeError` if `model` was not instantiated correctly.
    """
    if not model.context_params.logits_all:
        # Need every token's logits, not just the last token TODO: determine if we can
        # instead use a context manager to temporarily reset the attribute like we do in
        # cappr.huggingface. I'm not sure it's sufficient or sensible for llama_cpp.
        # Will need to read more of their code.
        raise TypeError("model needed to be instantiated with logits_all=True")


def _check_logits(logits) -> np.ndarray:
    """
    Returns back `logits` if there are no NaNs. Else raises a `TypeError`.
    """
    logits = np.array(logits)
    if np.isnan(logits).any():
        raise TypeError(
            "There are nan logits. This can happen if the model is re-loaded too many "
            "times in the same session. Please raise this as an issue so that I can "
            "investigate: https://github.com/kddubey/cappr/issues"
        )
    return logits


def token_logprobs(
    texts: Sequence[str],
    model: Llama,
    show_progress_bar: bool | None = None,
    add_bos: bool = False,
    **kwargs,
) -> list[list[float]]:
    """
    For each text, compute each token's log-probability conditional on all previous
    tokens in the text.

    Parameters
    ----------
    texts : Sequence[str]
        input texts
    model : Llama
        a model instantiated with ``logits_all=True``
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 texts
    add_bos : bool, optional
        whether or not to add a beginning-of-sentence token to all `texts`, by default
        False

    Returns
    -------
    log_probs : list[list[float]]
        `log_probs[text_idx][token_idx]` is the log-probability of the token at
        `token_idx` of `texts[text_idx]` conditional on all previous tokens in
        `texts[text_idx]`. If `texts[text_idx]` is a single token, then
        `log_probs[text_idx]` is `[None]`.

    Raises
    ------
    TypeError
        if `texts` is a string
    TypeError
        if `texts` is not a sequence
    ValueError
        if `texts` is empty
    """
    # Input checks
    if isinstance(texts, str):
        raise TypeError("texts cannot be a string. It must be a sequence of strings.")
    _check.nonempty_and_ordered(texts, variable_name="texts")
    _check_model(model)

    total = len(texts)
    if show_progress_bar is None:
        disable = total < _batch.MIN_TOTAL_FOR_SHOWING_PROGRESS_BAR
    else:
        disable = not show_progress_bar
    first_token_log_prob = [None]  # no CausalLM estimates Pr(token), so call it None
    # Loop through completions, b/c llama cpp currently doesn't support batch inference
    # Note: we could instead run logits_to_log_probs over a batch to save a bit of time,
    # but that'd cost more memory.
    log_probs = []
    for text in tqdm(texts, total=total, desc="marginal log-probs", disable=disable):
        input_ids = model.tokenize(text.encode("utf-8"), add_bos=add_bos)
        model.reset()  # clear the model's KV cache and logits
        model.eval(input_ids)
        log_probs_text: list[float] = logits_to_log_probs(
            _check_logits(model.eval_logits),
            np.array(input_ids),
            input_ids_start_idx=1,  # this token's log-prob is in the prev token's logit
            logits_end_idx=-1,
        ).tolist()
        log_probs.append(first_token_log_prob + log_probs_text)
    model.reset()
    return log_probs


def _log_probs_conditional_prompt_single_token_completions(
    prompt: str,
    input_ids_completions: Sequence[Sequence[int]],  # inner list is [token]
    model: Llama,
) -> list[list[float]]:
    """
    Runs a single inference on `prompt`.
    """
    _check_model(model)
    # 1. Clear the model's KV cache and logits (just in case)
    model.reset()
    # 2. Compute the prompt's next-token log-probs—a 1-D array
    input_ids_prompt = model.tokenize(prompt.encode("utf-8"), add_bos=True)
    model.eval(input_ids_prompt)
    prompt_next_token_log_probs = log_softmax(_check_logits(model.eval_logits[-1]))
    # 3. Grab each completion token's log-prob
    log_probs_completions: list[list[float]] = []
    for input_ids_completion in input_ids_completions:
        assert len(input_ids_completion) == 1
        input_id = input_ids_completion[0]
        log_probs_completions.append([prompt_next_token_log_probs[input_id]])
    model.reset()
    return log_probs_completions


def _log_probs_conditional_prompt(
    prompt: str,
    completions: Sequence[str],
    model: Llama,
) -> list[list[float]]:
    _check_model(model)
    # 1. Clear the model's KV cache and logits (just in case)
    model.reset()
    # 2. Set the model's KV cache to the prompt
    input_ids_prompt = model.tokenize(prompt.encode("utf-8"), add_bos=True)
    num_tokens_prompt = len(input_ids_prompt)
    model.eval(input_ids_prompt)
    # 3. Tokenize completions to determine whether or not we can do the single-token
    #    optimization. For Llama (and probably others) we don't want the completions to
    #    start w/ a bos token <s> b/c we need to mimic sending the prompt + completion
    #    together. For example, if 'a b' is the prompt and 'c' is the completion, the
    #    encoding should correspond to '<s> a b c' not '<s> a b <s> c'.
    input_ids_completions = [
        model.tokenize(completion.encode("utf-8"), add_bos=False)
        for completion in completions
    ]
    if all(
        len(input_ids_completion) == 1 for input_ids_completion in input_ids_completions
    ):
        return _log_probs_conditional_prompt_single_token_completions(
            prompt, input_ids_completions, model
        )
    # 4. Loop through completions, b/c llama cpp currently doesn't support batch
    #    inference
    log_probs_completions: list[list[float]] = []
    for input_ids_completion in input_ids_completions:
        # 4.1. Given the prompt, compute all next-token logits for each completion
        #      token.
        model.eval(input_ids_completion)
        # 4.2. Logits -> log-probs. We need the prompt's last token's logits b/c it
        # contains the first completion token's log-prob. But we don't need the last
        # completion token's next-token logits ofc. Also, it's num_tokens_prompt - 1 b/c
        # of 0-indexing.
        logits_completion = _check_logits(model.eval_logits)[num_tokens_prompt - 1 : -1]
        log_probs_completion: list[float] = logits_to_log_probs(
            logits_completion, np.array(input_ids_completion)
        ).tolist()
        log_probs_completions.append(log_probs_completion)
        # 4.3. Most critical step: reset the model's KV cache to the prompt! Without
        #      this line, the cache would include this completion's KVs, which is mega
        #      wrong.
        model.n_tokens = num_tokens_prompt
    model.reset()
    return log_probs_completions


@classify._log_probs_conditional
def log_probs_conditional(
    prompts: str | Sequence[str],
    completions: Sequence[str],
    model: Llama,
    show_progress_bar: bool | None = None,
    **kwargs,
) -> list[list[float]] | list[list[list[float]]]:
    """
    Log-probabilities of each completion token conditional on each prompt and previous
    completion tokens.

    Parameters
    ----------
    prompts : str | Sequence[str]
        string(s), where, e.g., each contains the text you want to classify
    completions : Sequence[str]
        strings, where, e.g., each one is the name of a class which could come after a
        prompt
    model : Llama
        a model instantiated with ``logits_all=True``
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 prompts

    Returns
    -------
    log_probs_completions : list[list[float]] | list[list[list[float]]]

        If `prompts` is a string, then a 2-D list is returned:
        `log_probs_completions[completion_idx][completion_token_idx]` is the
        log-probability of the completion token in `completions[completion_idx]`,
        conditional on `prompt` and previous completion tokens.

        If `prompts` is a sequence of strings, then a 3-D list is returned:
        `log_probs_completions[prompt_idx][completion_idx][completion_token_idx]` is the
        log-probability of the completion token in `completions[completion_idx]`,
        conditional on `prompts[prompt_idx]` and previous completion tokens.

    Note
    ----
    To efficiently aggregate `log_probs_completions`, use
    :func:`cappr.utils.classify.agg_log_probs`.

    Example
    -------
    Here we'll use single characters (which are of course single tokens) to more clearly
    demonstrate what this function does::

        from llama_cpp import Llama
        from cappr.llama_cpp.classify import log_probs_conditional

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Create data
        prompts = ["x y", "a b c"]
        completions = ["z", "d e"]

        # Compute
        log_probs_completions = log_probs_conditional(
            prompts, completions, model
        )

        # Outputs (rounded) next to their symbolic representation

        print(log_probs_completions[0])
        # [[-12.8],        [[log Pr(z | x, y)],
        #  [-10.8, -10.7]]  [log Pr(d | x, y),    log Pr(e | x, y, d)]]

        print(log_probs_completions[1])
        # [[-9.5],        [[log Pr(z | a, b, c)],
        #  [-9.9, -10.0]]  [log Pr(d | a, b, c), log Pr(e | a, b, c, d)]]
    """
    total = len(prompts)
    if show_progress_bar is None:
        disable = total < _batch.MIN_TOTAL_FOR_SHOWING_PROGRESS_BAR
    else:
        disable = not show_progress_bar
    desc = "conditional log-probs"
    return [
        _log_probs_conditional_prompt(prompt, completions, model)
        for prompt in tqdm(prompts, total=total, disable=disable, desc=desc)
    ]


@classify._log_probs_conditional_examples
def log_probs_conditional_examples(
    examples: Example | Sequence[Example],
    model: Llama,
    show_progress_bar: bool | None = None,
) -> list[list[float]] | list[list[list[float]]]:
    """
    Log-probabilities of each completion token conditional on each prompt and previous
    completion tokens.

    Parameters
    ----------
    examples : Example | Sequence[Example]
        `Example` object(s), where each contains a prompt and its set of possible
        completions
    model : Llama
        a model instantiated with ``logits_all=True``
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 `examples`

    Returns
    -------
    log_probs_completions : list[list[float]] | list[list[list[float]]]

        If `examples` is a :class:`cappr.Example`, then a 2-D list is returned:
        `log_probs_completions[completion_idx][completion_token_idx]` is the
        log-probability of the completion token in
        `example.completions[completion_idx]`, conditional on `example.prompt` and
        previous completion tokens.

        If `examples` is a sequence of :class:`cappr.Example` objects, then a 3-D list
        is returned:
        `log_probs_completions[example_idx][completion_idx][completion_token_idx]` is
        the log-probability of the completion token in
        `examples[example_idx].completions[completion_idx]`, conditional on
        `examples[example_idx].prompt` and previous completion tokens.

    Note
    ----
    To aggregate `log_probs_completions`, use
    :func:`cappr.utils.classify.agg_log_probs`.

    Note
    ----
    The attributes :attr:`cappr.Example.end_of_prompt` and :attr:`cappr.Example.prior`
    are unused.

    Example
    -------
    Here we'll use single characters (which are of course single tokens) to more clearly
    demonstrate what this function does::

        from llama_cpp import Llama
        from cappr import Example
        from cappr.llama_cpp.classify import log_probs_conditional_examples

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Create examples
        examples = [
            Example(prompt="x y", completions=("z", "d e")),
            Example(prompt="a b c", completions=("d e",), normalize=False),
        ]

        # Compute
        log_probs_completions = log_probs_conditional_examples(
            examples, model
        )

        # Outputs (rounded) next to their symbolic representation

        print(log_probs_completions[0])  # corresponds to examples[0]
        # [[-12.8],        [[log Pr(z | x, y)],
        #  [-10.8, -10.7]]  [log Pr(d | x, y),    log Pr(e | x, y, d)]]

        print(log_probs_completions[1])  # corresponds to examples[1]
        # [[-9.90, -10.0]] [[log Pr(d | a, b, c)], log Pr(e | a, b, c, d)]]
    """
    # Little weird. I want my IDE to know that examples is always a Sequence[Example]
    # b/c of the decorator.
    examples: Sequence[Example] = examples
    total = len(examples)
    if show_progress_bar is None:
        disable = total < _batch.MIN_TOTAL_FOR_SHOWING_PROGRESS_BAR
    else:
        disable = not show_progress_bar
    desc = "conditional log-probs"
    return [
        _log_probs_conditional_prompt(example.prompt, example.completions, model)
        for example in tqdm(examples, total=total, disable=disable, desc=desc)
    ]


@classify._predict_proba
def predict_proba(
    prompts: str | Sequence[str],
    completions: Sequence[str],
    model: Llama,
    prior: Sequence[float] | None = None,
    normalize: bool = True,
    discount_completions: float = 0.0,
    log_marg_probs_completions: Sequence[Sequence[float]] | None = None,
    show_progress_bar: bool | None = None,
) -> npt.NDArray[np.floating]:
    """
    Predict probabilities of each completion coming after each prompt.

    Parameters
    ----------
    prompts : str | Sequence[str]
        string(s), where, e.g., each contains the text you want to classify
    completions : Sequence[str]
        strings, where, e.g., each one is the name of a class which could come after a
        prompt
    model : Llama
        a model instantiated with ``logits_all=True``
    prior : Sequence[float] | None, optional
        a probability distribution over `completions`, representing a belief about their
        likelihoods regardless of the prompt. By default, each completion in
        `completions` is assumed to be equally likely
    normalize : bool, optional
        whether or not to normalize completion-after-prompt probabilities into a
        probability distribution over completions. Set this to `False` if you'd like the
        raw completion-after-prompt probability, or you're solving a multi-label
        prediction problem. By default, True
    discount_completions : float, optional
        experimental feature: set it (e.g., 1.0 may work well) if a completion is
        consistently getting too high predicted probabilities. You could instead fudge
        the `prior`, but this hyperparameter may be easier to tune than the `prior`. By
        default 0.0
    log_marg_probs_completions : Sequence[Sequence[float]] | None, optional
        experimental feature: pre-computed log probabilities of completion tokens
        conditional on previous completion tokens (not prompt tokens). Only used if `not
        discount_completions`. Pre-compute them by passing `completions` and `model` to
        :func:`token_logprobs`. By default, if `not discount_completions`, they are
        (re-)computed
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 prompts

    Returns
    -------
    pred_probs : npt.NDArray[np.floating]

        If `prompts` is a string, then an array with shape `len(completions),` is
        returned: `pred_probs[completion_idx]` is the model's estimate of the
        probability that `completions[completion_idx]` comes after `prompt`.

        If `prompts` is a sequence of strings, then an array with shape `(len(prompts),
        len(completions))` is returned: `pred_probs[prompt_idx, completion_idx]` is the
        model's estimate of the probability that `completions[completion_idx]` comes
        after `prompts[prompt_idx]`.

    Note
    ----
    In this function, the set of possible completions which could follow each prompt is
    the same for every prompt. If instead, each prompt could be followed by a
    *different* set of completions, then construct a sequence of :class:`cappr.Example`
    objects and pass them to :func:`predict_proba_examples`.

    Example
    -------
    Let's have our little Llama predict some story beginnings::

        from llama_cpp import Llama
        from cappr.llama_cpp.classify import predict_proba

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Define a classification task
        prompts = ["In a hole in", "Once upon"]
        completions = ("a time", "the ground")

        # Compute
        pred_probs = predict_proba(prompts, completions, model)

        pred_probs_rounded = pred_probs.round(2)  # just for cleaner output

        # predicted probability that the ending for the clause
        # "In a hole in" is "the ground"
        print(pred_probs_rounded[0, 1])
        # 0.98

        # predicted probability that the ending for the clause
        # "Once upon" is "a time"
        print(pred_probs_rounded[1, 0])
        # 1.0
    """
    return log_probs_conditional(**locals())


@classify._predict_proba_examples
def predict_proba_examples(
    examples: Example | Sequence[Example],
    model: Llama,
    show_progress_bar: bool | None = None,
) -> npt.NDArray[np.floating] | list[npt.NDArray[np.floating]]:
    """
    Predict probabilities of each completion coming after each prompt.

    Parameters
    ----------
    examples : Example | Sequence[Example]
        `Example` object(s), where each contains a prompt and its set of possible
        completions
    model : Llama
        a model instantiated with ``logits_all=True``
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 `examples`

    Returns
    -------
    pred_probs : npt.NDArray[np.floating] | list[npt.NDArray[np.floating]]

        If `examples` is an :class:`cappr.Example`, then an array with shape
        `(len(example.completions),)` is returned: `pred_probs[completion_idx]` is the
        model's estimate of the probability that `example.completions[completion_idx]`
        comes after `example.prompt`.

        If `examples` is a sequence of :class:`cappr.Example` objects, then a list with
        length `len(examples)` is returned: `pred_probs[example_idx][completion_idx]` is
        the model's estimate of the probability that
        `examples[example_idx].completions[completion_idx]` comes after
        `examples[example_idx].prompt`. If the number of completions per example is a
        constant `k`, then an array with shape `(len(examples), k)` is returned instead
        of a list of 1-D arrays.

    Note
    ----
    The attribute :attr:`cappr.Example.end_of_prompt` is unused.

    Example
    -------
    Some story analysis::

        from llama_cpp import Llama
        from cappr import Example
        from cappr.llama_cpp.classify import predict_proba_examples

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Create examples
        examples = [
            Example(
                prompt="Story: I enjoyed pizza with my buddies.\\nMoral:",
                completions=("make friends", "food is yummy", "absolutely nothing"),
                prior=(2 / 5, 2 / 5, 1 / 5),
            ),
            Example(
                prompt="The child rescued the animal. The child is a",
                completions=("hero", "villain"),
            ),
        ]

        # Compute
        pred_probs = predict_proba_examples(examples, model)

        # predicted probability that the moral of the 1st story is that food is yummy
        print(pred_probs[0][1].round(2))
        # 0.72

        # predicted probability that the hero of the 2nd story is the child
        print(pred_probs[1][0].round(2))
        # 0.95
    """
    return log_probs_conditional_examples(**locals())


@classify._predict
def predict(
    prompts: str | Sequence[str],
    completions: Sequence[str],
    model: Llama,
    prior: Sequence[float] | None = None,
    discount_completions: float = 0.0,
    log_marg_probs_completions: Sequence[Sequence[float]] | None = None,
    show_progress_bar: bool | None = None,
) -> str | list[str]:
    """
    Predict which completion is most likely to follow each prompt.

    Parameters
    ----------
    prompts : str | Sequence[str]
        string(s), where, e.g., each contains the text you want to classify
    completions : Sequence[str]
        strings, where, e.g., each one is the name of a class which could come after a
        prompt
    model : Llama
        a model instantiated with ``logits_all=True``
    prior : Sequence[float] | None, optional
        a probability distribution over `completions`, representing a belief about their
        likelihoods regardless of the prompt. By default, each completion in
        `completions` is assumed to be equally likely
    discount_completions : float, optional
        experimental feature: set it to >0.0 (e.g., 1.0 may work well) if a completion
        is consistently getting over-predicted. You could instead fudge the `prior`, but
        this hyperparameter may be easier to tune than the `prior`. By default 0.0
    log_marg_probs_completions : Sequence[Sequence[float]] | None, optional
        experimental feature: pre-computed log probabilities of completion tokens
        conditional on previous completion tokens (not prompt tokens). Only used if `not
        discount_completions`. Pre-compute them by passing `completions` and `model` to
        :func:`token_logprobs`. By default, if `not discount_completions`, they are
        (re-)computed
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 prompts

    Returns
    -------
    preds : str | list[str]

        If `prompts` is a string, then the completion from `completions` which is
        predicted to most likely follow `prompt` is returned.

        If `prompts` is a sequence of strings, then a list with length `len(prompts)` is
        returned. `preds[prompt_idx]` is the completion in `completions` which is
        predicted to follow `prompts[prompt_idx]`.

    Note
    ----
    In this function, the set of possible completions which could follow each prompt is
    the same for every prompt. If instead, each prompt could be followed by a
    *different* set of completions, then construct a sequence of :class:`cappr.Example`
    objects and pass them to :func:`predict_examples`.

    Example
    -------
    Let's have our little Llama predict some story beginnings::

        from llama_cpp import Llama
        from cappr.llama_cpp.classify import predict

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Define a classification task
        prompts = ["In a hole in", "Once upon"]
        completions = ("a time", "the ground")

        # Compute
        preds = predict(prompts, completions, model)

        # Predicted ending for the first clause: "In a hole in"
        print(preds[0])
        # the ground

        # Predicted ending for the first clause: "Once upon"
        print(preds[1])
        # a time
    """
    return predict_proba(**locals())


@classify._predict_examples
def predict_examples(
    examples: Example | Sequence[Example],
    model: Llama,
    show_progress_bar: bool | None = None,
) -> str | list[str]:
    """
    Predict which completion is most likely to follow each prompt.

    Parameters
    ----------
    examples : Example | Sequence[Example]
        `Example` object(s), where each contains a prompt and its set of possible
        completions
    model : Llama
        a model instantiated with ``logits_all=True``
    show_progress_bar : bool | None, optional
        whether or not to show a progress bar. By default, it will be shown only if
        there are at least 5 `examples`

    Returns
    -------
    preds : str | list[str]

        If `examples` is an :class:`cappr.Example`, then the completion from
        `example.completions` which is predicted to most likely follow `example.prompt`
        is returned.

        If `examples` is a sequence of :class:`cappr.Example` objects, then a list with
        length `len(examples)` is returned: `preds[example_idx]` is the completion in
        `examples[example_idx].completions` which is predicted to most likely follow
        `examples[example_idx].prompt`.

    Note
    ----
    The attribute :attr:`cappr.Example.end_of_prompt` is unused.

    Example
    -------
    Some story analysis::

        from llama_cpp import Llama
        from cappr import Example
        from cappr.llama_cpp.classify import predict_examples

        # Load model
        # The top of this page has instructions to download this model
        model_path = "./TinyLLama-v0.Q8_0.gguf"
        # Always set logits_all=True for CAPPr
        model = Llama(model_path, logits_all=True, verbose=False)

        # Create examples
        examples = [
            Example(
                prompt="Story: I enjoyed pizza with my buddies.\\nMoral:",
                completions=("make friends", "food is yummy", "absolutely nothing"),
                prior=(2 / 5, 2 / 5, 1 / 5),
            ),
            Example(
                prompt="The child rescued the animal. The child is a",
                completions=("hero", "villain"),
            ),
        ]

        # Compute
        preds = predict_examples(examples, model)

        # the moral of the 1st story
        print(preds[0])
        # food is yummy

        # the character of the 2nd story
        print(preds[1])
        # hero

    """
    return predict_proba_examples(**locals())
