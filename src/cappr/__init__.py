"""
Completion After Prompt Probability
"""
__version__ = "0.3.0"

from . import utils
from ._example import Example
from . import openai

try:
    from . import huggingface  # optional
except ModuleNotFoundError:
    pass
