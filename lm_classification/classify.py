'''
Perform prompt-completion classification: for a given prompt and completion,
what's the probability that the completion follows the prompt?

Only supports LMs which you gotta pay for in
[OpenAI's text completion API](https://platform.openai.com/docs/models/gpt-3).
'''
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np
import openai
from tqdm.auto import tqdm

from lm_classification import utils


END_OF_PROMPT = ' <|endoftext|>\n\n'
## IIUC these tokens were used to separate prompts from completions during
## training. The second \n is iffy, but seems to be common enough based on my
## experience with the completion endpoint.


def gpt3_log_probs(texts: Sequence[str], model: str='text-ada-001',
                   batch_size: int=20) -> list[list[float]]:
    '''
    Returns a list `log_probs` where `log_probs[i]` is the value of
    `'log_probs' -> 'token_logprobs'` (from the OpenAI Completion endpoint) for
    `texts[i]` using `model`.
    '''
    if batch_size > 20:
        raise ValueError('batch_size must be <= 20.')
    if isinstance(texts, str):
        ## Passing in a string will silently but majorly fail. Handle it
        texts = [texts]
    log_probs = []
    with tqdm(total=len(texts), desc='Computing probs') as progress_bar:
        for texts_batch in utils.batch(texts, batch_size):
            response = utils.openai_method_retry(openai.Completion.create,
                                                 model=model,
                                                 prompt=texts_batch,
                                                 ## rest should be hard-coded
                                                 max_tokens=0,
                                                 logprobs=1,
                                                 echo=True)
            log_probs.extend([choice['logprobs']['token_logprobs']
                              for choice in response['choices']])
            progress_bar.update(len(texts_batch))
    return log_probs


def log_probs_completions(completions: Sequence[str],
                          log_probs: Sequence[Sequence[float]]):
    '''
    Returns a list `log_probs_completions` where `log_probs_completions[i]` is a
    list of conditional log-probablities for each token in `completions[i]`,
    extracted by slicing `log_probs[i]`.
    '''
    if len(completions) != len(log_probs):
        raise ValueError( 'Different number of completions and log_probs: '
                         f'{len(completions)}, {len(log_probs)}.')
    log_probs_completions: list[list[float]] = []
    for completion, log_probs in zip(completions, log_probs):
        num_completion_tokens = len(utils.gpt2_tokenizer(completion)
                                    ['input_ids'])
        log_probs_completions.append(log_probs[-num_completion_tokens:])
    return log_probs_completions


def log_probs_conditional(prompts: Sequence[str],
                          completions: Sequence[str],
                          model: str='text-ada-001',
                          batch_size: int=20,
                          end_of_prompt: str=END_OF_PROMPT):
    '''
    Returns a list `log_probs_completions` where `log_probs_completions[i][j]`
    is a list of the `model`'s estimates of log-probablities of each token in
    `completions[j]`, conditional on previous tokens in the completion and
    `prompts[i]`.
    '''
    ## str / non-Sequence[str] inputs silently, wastefully, and irreparably fail
    if isinstance(prompts, str) or not isinstance(prompts, Sequence):
        raise TypeError('prompts must be a Sequence of strings.')
    if isinstance(completions, str) or not isinstance(completions, Sequence):
        raise TypeError('completions must be a Sequence of strings.')
    ## Flat list of prompts and their completions. Will post-process
    texts = [prompt + end_of_prompt + completion
             for prompt in prompts
             for completion in completions]
    log_probs = gpt3_log_probs(texts, model=model, batch_size=batch_size)
    ## Since log_probs is a flat list, we'll need to batch them by the size and
    ## order of completions to fulfill the spec.
    return [log_probs_completions(completions, log_probs_batch)
            for log_probs_batch
            in utils.batch(log_probs, size=len(completions))]


def _check_prior(prior: Optional[Sequence[float]]=None):
    '''
    Raises an error if `prior` is not a 1-D `Sequence` which sums to 1.
    '''
    if prior is None: ## it's a uniform prior, no need to check anything
        return None
    if not isinstance(prior, Sequence):
        raise TypeError('prior must be a Sequence.')
    if len(np.shape(prior)) != 1:
        raise ValueError('prior must be 1-D.')
    prior_arr: np.ndarray = np.array(prior, dtype=float) ## try casting to float
    if not np.isclose(prior_arr.sum(), 1, rtol=0, atol=1e-6):
        raise ValueError('prior must sum to 1 (tol 1e-6).')


@dataclass(frozen=True)
class Example:
    '''
    Represents a single test example for a prompt-completion text classification
    task. This data structure is useful when different examples from the same
    dataset may belong to different classes.
    This applies to, e.g., [COPA](https://people.ict.usc.edu/~gordon/copa.html).

    `prompt`: cointains the text to classify, perhaps with instructions
    `completions`: possible completions/answers to the `prompt`
    `prior`: (optional) a probability distribution over `completions`.
    '''
    prompt: str
    completions: Sequence[str]
    prior: Optional[Sequence[float]]=None

    def __post_init__(self):
        ## Check inputs here so that fxns of Example don't need to check
        if not isinstance(self.prompt, str):
            raise TypeError('prompt must be a string.')
        if (isinstance(self.completions, str) or
            not isinstance(self.completions, Sequence)):
            raise TypeError('completions must be a Sequence of strings.')
        _check_prior(self.prior)
        if self.prior is not None and len(self.completions) != len(self.prior):
            raise ValueError( 'completions and prior are different lengths: '
                             f'{len(self.completions)}, {len(self.prior)}.')


def log_probs_conditional_examples(examples: Sequence[Example],
                                   model: str='text-ada-001',
                                   batch_size: int=20,
                                   end_of_prompt: str=END_OF_PROMPT
                                  ) -> list[list[list[float]]]:
    '''
    Returns a list `log_probs_completions` where `log_probs_completions[i][j]`
    is a list of the `model`'s estimates of log-probablities of each token in
    `examples[i].completions[j]`, conditional on previous tokens in the
    completion and `examples[i].prompt`.
    '''
    ## Flat list of prompts and their completions. Will post-process
    texts = [example.prompt + end_of_prompt + completion
             for example in examples
             for completion in example.completions]
    log_probs_all = gpt3_log_probs(texts, model=model, batch_size=batch_size)
    ## Flatten completions in same order as examples were flattened
    completions_all = [completion for example in examples
                       for completion in example.completions]
    log_probs_completions_all = log_probs_completions(completions_all,
                                                      log_probs_all)
    ## Batch by completions to fulfill the spec
    completions_sizes = [len(example.completions) for example in examples]
    return list(utils.batch_variable(log_probs_completions_all,
                                     sizes=completions_sizes))


def agg_log_probs(log_probs: Sequence[Sequence[Sequence[float]]],
                  func: Callable[[Sequence[float]], float]=np.mean
                 ) -> list[list[float]]:
    '''
    Returns a list, `likelihoods`, where `likelihoods[i][j]` is
    `np.exp(func(log_probs[i][j]))`.
    '''
    ## TODO: any elegant way to vectorize? Problem is that `log_probs` can be
    ## ragged along the 2nd *and* 3rd dimensions.
    return [[np.exp(func(log_probs_class))
             for log_probs_class in log_probs_classes]
            for log_probs_classes in log_probs]


def posterior_prob(likelihoods: np.ndarray, axis: int,
                   prior: Optional[Sequence[float]]=None) -> np.ndarray:
    '''
    Returns an array, `posteriors`, where `posteriors[i]` is the normalized
    probability distribution of `likelihoods[i] * prior`. If `prior is None`,
    then a uniform prior is applied, i.e., `posteriors[i]` is simply a
    normalized copy of `likelihoods[i]`.

    Set `axis` to the axis over which the distribution is defined, e.g., `0` if
    likelihoods is 1-D. 
    '''
    likelihoods = np.array(likelihoods)
    if prior is None:
        return likelihoods / likelihoods.sum(axis=axis, keepdims=True)
    _check_prior(prior)
    posteriors_unnorm: np.ndarray = likelihoods * prior
    return posteriors_unnorm / posteriors_unnorm.sum(axis=axis, keepdims=True)


def predict_proba(prompts: Sequence[str], completions: Sequence[str],
                  prior: Optional[Sequence[float]]=None,
                  model: str='text-ada-001', batch_size: int=20):
    '''
    Returns an array with shape `(len(prompts), len(completions))` called
    `pred_probs`, where `pred_probs[i, j]` is a `model`'s estimate of
    Pr(`completions[j]` | `prompts[i]`).
    '''
    if prior is not None and len(completions) != len(prior):
        raise ValueError( 'completions and prior are different lengths: '
                         f'{len(completions)}, {len(prior)}.')
    log_probs_all = log_probs_conditional(prompts, completions, model=model,
                                          batch_size=batch_size)
    likelihoods = agg_log_probs(log_probs_all)
    return posterior_prob(likelihoods, axis=1, prior=prior)


def predict_proba_examples(examples: Sequence[Example],
                           model: str='text-ada-001',
                           batch_size: int=20):
    '''
    Returns a list, `pred_probs`, where `pred_probs[i][j]` is a `model`'s
    estimate of Pr(`examples[i].completions[j]` | `examples[i].prompt`).

    If the number of completions per example is constant, an array with shape
    `(len(examples), len(examples[0].completions))` is returned instead.
    '''
    log_probs_all = log_probs_conditional_examples(examples, model=model,
                                                   batch_size=batch_size)
    likelihoods_all = agg_log_probs(log_probs_all)
    pred_probs = [posterior_prob(likelihoods, axis=0, prior=example.prior)
                  for likelihoods, example in zip(likelihoods_all, examples)]
    ## For convenence sake, convert to array if possible
    completions_sizes_set = {len(example.completions) for example in examples}
    if len(completions_sizes_set) == 1:
        return np.array(pred_probs)
    else:
        return pred_probs
