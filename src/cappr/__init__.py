"""
Completion After Prompt Probability
"""
__version__ = "0.5.0"

from . import utils
from ._example import Example
from . import openai

try:
    from . import huggingface  # optional
except ModuleNotFoundError:
    pass

try:
    from . import llama_cpp  # optional
except ModuleNotFoundError:
    pass
