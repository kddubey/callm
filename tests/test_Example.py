"""
Unit tests `cappr.Example` input checks.
"""
from __future__ import annotations
from typing import Any

import numpy as np
import pandas as pd
import pytest

from cappr import Example


def modify_kwargs(kwargs: dict[str, Any], **kwargs_new) -> dict[str, Any]:
    return {**kwargs, **kwargs_new}


@pytest.mark.parametrize("prompt", ("hi",))
@pytest.mark.parametrize("completions", (["completion won", "completion tew"],))
@pytest.mark.parametrize("end_of_prompt", (" ",))
@pytest.mark.parametrize("prior", (None, [1 / 3, 2 / 3], np.array([0.5, 0.5])))
@pytest.mark.parametrize("normalize", (True, False))
def test___post_init__(prompt, completions, end_of_prompt, prior, normalize):
    kwargs_valid = dict(
        prompt=prompt,
        completions=completions,
        end_of_prompt=end_of_prompt,
        prior=prior,
        normalize=normalize,
    )
    # Valid Examples
    Example(**kwargs_valid)
    Example(
        **modify_kwargs(
            kwargs_valid,
            completions=pd.Series(
                completions,
                index=np.random.choice(len(completions), size=len(completions)),
            ),
        )
    )

    # Invalid Examples

    # prompt - empty
    with pytest.raises(ValueError, match="prompt must be non-empty."):
        Example(**modify_kwargs(kwargs_valid, prompt=""))
    # prompt - not a string
    with pytest.raises(TypeError, match="prompt must be a string."):
        Example(**modify_kwargs(kwargs_valid, prompt=[prompt]))

    # end_of_prompt - non-" "/""
    _msg = 'end_of_prompt must be a whitespace " " or an empty string "".'
    with pytest.raises(TypeError, match=_msg):
        Example(**modify_kwargs(kwargs_valid, end_of_prompt=None))
    with pytest.raises(ValueError, match=_msg):
        Example(**modify_kwargs(kwargs_valid, end_of_prompt=": "))

    # completions - empty
    with pytest.raises(ValueError, match="completions must be non-empty."):
        Example(**modify_kwargs(kwargs_valid, completions=[]))
    # completions - non-ordered
    with pytest.raises(TypeError, match="completions must be an ordered collection."):
        Example(**modify_kwargs(kwargs_valid, completions=set(completions)))
    # completions - string
    with pytest.raises(TypeError, match="completions cannot be a string."):
        Example(**modify_kwargs(kwargs_valid, completions=completions[0]))

    # prior - probabilities < 0
    with pytest.raises(
        ValueError, match="prior must contain probabilities between 0 and 1."
    ):
        Example(**modify_kwargs(kwargs_valid, prior=[-1] * len(completions)))
    # prior - probabilities > 1
    with pytest.raises(
        ValueError, match="prior must contain probabilities between 0 and 1."
    ):
        Example(**modify_kwargs(kwargs_valid, prior=[1.1] * len(completions)))
