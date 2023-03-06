'''
Unit tests `lm_classification.classify`.
'''
from __future__ import annotations
import pytest

import numpy as np
import tiktoken

from lm_classification import classify


tokenizer = tiktoken.get_encoding('gpt2')


@pytest.fixture(scope='module')
def model():
    '''
    This name is intentionally *not* an OpenAI API model. That's to prevent
    un-mocked API calls from going through. API calls must be mocked.
    '''
    return '🦖 ☄️ 💥'


@pytest.fixture(scope='module')
def prompts():
    return ['Fill in the blank. Have an __ day!',
            'i before e except after',
            'Popular steak sauce:',
            'The English alphabet: a, b,']


@pytest.fixture(scope='module')
def completions():
    return ['A1', 'c']


@pytest.fixture(scope='module')
def examples():
    ## Let's make these ragged (different # completions for each prompt), since
    ## that's the use case for a classify.Example
    return [classify.Example(prompt='I am currently',
                             completions=('(っ◔◡◔)っ ♥ unemployed ♥',
                                          'boooooo'),
                             prior=(1/2, 1/2)),
            classify.Example(prompt='🎶The best time to wear a striped sweater',
                             completions=('is all the time🎶',)),
            classify.Example(prompt='machine',
                             completions=('-washed',
                                          ' learnt',
                                          ' 🤖'),
                             prior=(1/6, 2/3, 1/6),
                             end_of_prompt='')]


def _log_probs(texts: list[str]) -> list[list[float]]:
    '''
    Returns a list `log_probs` where `log_probs[i]` is a list of random
    log-probabilities whose length is the number of tokens in `texts[i]`.
    '''
    sizes = [len(tokenizer.encode(text)) for text in texts]
    return [list(np.log(np.random.uniform(size=size)))
            for size in sizes]


def mock_openai_method_retry(openai_method, prompt, **kwargs):
    ## Technically, we should return a openai.openai_object.OpenAIObject
    ## For now, just gonna return the minimum dict required
    token_logprobs_batch = _log_probs(prompt)
    return {'choices': [{'logprobs': {'token_logprobs': list(token_logprobs)}}
                        for token_logprobs in token_logprobs_batch]}


@pytest.mark.parametrize('texts', (['a b', 'c'],))
def test_gpt_log_probs(mocker, texts, model):
    ## We ofc shouldn't actually hit any endpoints during testing
    mocker.patch('lm_classification.utils.api.openai_method_retry',
                 mock_openai_method_retry)
    log_probs = classify.gpt_log_probs(texts, model)
    ## Since the endpoint is mocked out, the only thing left to test is the
    ## the list-extend loop. Namely, check the overall size, and types
    assert len(log_probs) == len(texts)
    assert isinstance(log_probs, list)
    for log_probs_text in log_probs:
        assert isinstance(log_probs_text, list)
        for log_prob in log_probs_text:
            assert isinstance(log_prob, float)


def mock_tiktoken_encoding_for_model(model):
    return tokenizer


@pytest.mark.parametrize('log_probs', ([list(range(10)), list(range(10))],))
def test_log_probs_completions(mocker, completions, log_probs, model):
    mocker.patch('tiktoken.encoding_for_model',
                 mock_tiktoken_encoding_for_model)
    log_probs_completions = classify.log_probs_completions(completions,
                                                           log_probs, model)
    assert log_probs_completions == [[8,9], [9]]


def mock_gpt_log_probs(texts, model, **kwargs):
    return _log_probs(texts)


def mock_log_probs_completions(completions, log_probs, model):
    return _log_probs(completions)


def test_log_probs_conditional(mocker, prompts, completions, model):
    mocker.patch('lm_classification.classify.gpt_log_probs',
                 mock_gpt_log_probs)
    mocker.patch('lm_classification.classify.log_probs_completions',
                 mock_log_probs_completions)
    log_probs_conditional = classify.log_probs_conditional(prompts, completions,
                                                           model)
    assert len(log_probs_conditional) == len(prompts)
    for log_probs_prompt in log_probs_conditional:
        assert len(log_probs_prompt) == len(completions)
        for log_probs_flat, completion in zip(log_probs_prompt, completions):
            assert len(log_probs_flat) == len(tokenizer.encode(completion))


def test_log_probs_conditional_examples(mocker,
                                        examples: list[classify.Example],
                                        model):
    mocker.patch('lm_classification.classify.gpt_log_probs',
                 mock_gpt_log_probs)
    mocker.patch('lm_classification.classify.log_probs_completions',
                 mock_log_probs_completions)
    log_probs_conditional = classify.log_probs_conditional_examples(examples,
                                                                    model)
    assert len(log_probs_conditional) == len(examples)
    for log_probs_prompt, example in zip(log_probs_conditional, examples):
        completions = example.completions
        assert len(log_probs_prompt) == len(completions)
        for log_probs_flat, completion in zip(log_probs_prompt, completions):
            assert len(log_probs_flat) == len(tokenizer.encode(completion))


@pytest.mark.parametrize('log_probs', ([[[2,2], [1]], [[1/2, 1/2], [4]]],))
def test_agg_log_probs(mocker, log_probs):
    mocker.patch('numpy.exp', lambda x: x)
    log_probs_agg = classify.agg_log_probs(log_probs, func=sum)
    assert log_probs_agg == [[4,1], [1,4]]


@pytest.mark.parametrize('likelihoods', ([[4,1], [1,4]],))
@pytest.mark.parametrize('prior', (None, [1/2, 1/2], [1/3, 2/3]))
@pytest.mark.parametrize('normalize', (True, False))
def test_posterior_prob_2d(likelihoods, prior, normalize):
    posteriors = classify.posterior_prob(likelihoods, axis=1, prior=prior,
                                         normalize=normalize)
    if prior == [1/2, 1/2]: ## hard-coded b/c idk how to engineer tests
        if normalize:
            assert np.all(np.isclose(posteriors, [[4/5, 1/5], [1/5, 4/5]]))
        else:
            assert np.all(posteriors == np.array(likelihoods)/2)
    elif prior is None:
        if normalize:
            assert np.all(np.isclose(posteriors, [[4/5, 1/5], [1/5, 4/5]]))
        else:
            assert np.all(posteriors == likelihoods)
    elif prior == [1/3, 2/3]:
        if normalize:
            assert np.all(np.isclose(posteriors, [[2/3, 1/3], [1/9, 8/9]]))
        else:
            assert np.all(np.isclose(posteriors, [[4/3, 2/3], [1/3, 8/3]]))
    else:
        raise ValueError('nooo')


@pytest.mark.parametrize('likelihoods', ([4,1],))
@pytest.mark.parametrize('prior', (None, [1/2, 1/2], [1/3, 2/3]))
@pytest.mark.parametrize('normalize', (True, False))
def test_posterior_prob_1d(likelihoods: np.ndarray, prior, normalize):
    posteriors = classify.posterior_prob(likelihoods, axis=0, prior=prior,
                                         normalize=normalize)
    if prior == [1/2, 1/2]: ## hard-coded b/c idk how to engineer tests
        if normalize:
            assert np.all(np.isclose(posteriors, [4/5, 1/5]))
        else:
            assert np.all(posteriors == np.array(likelihoods)/2)
    elif prior is None:
        if normalize:
            assert np.all(np.isclose(posteriors, [4/5, 1/5]))
        else:
            assert np.all(posteriors == likelihoods)
    elif prior == [1/3, 2/3]:
        if normalize:
            assert np.all(np.isclose(posteriors, [2/3, 1/3]))
        else:
            assert np.all(np.isclose(posteriors, [4/3, 2/3]))
    else:
        raise ValueError('nooo')


def mock_log_probs_conditional(prompts, completions, model, **kwargs):
    return [_log_probs(completions) for _ in prompts]


def test_predict_proba(mocker, prompts, completions, model):
    mocker.patch('lm_classification.classify.log_probs_conditional',
                 mock_log_probs_conditional)
    pred_probs = classify.predict_proba(prompts, completions, model)
    ## As a unit test, only the shape needs to be tested
    assert pred_probs.shape == (len(prompts), len(completions))


def mock_log_probs_conditional_examples(examples: list[classify.Example],
                                        model, **kwargs):
    return [_log_probs(example.completions) for example in examples]


def test_predict_proba_examples(mocker, examples: list[classify.Example],
                                model):
    mocker.patch('lm_classification.classify.log_probs_conditional_examples',
                 mock_log_probs_conditional_examples)
    pred_probs = classify.predict_proba_examples(examples, model)
    ## As a unit test, only the shape needs to be tested
    assert len(pred_probs) == len(examples)
    for pred_prob_example, example in zip(pred_probs, examples):
        assert len(pred_prob_example) == len(example.completions)
