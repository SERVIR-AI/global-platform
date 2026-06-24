"""Load the agent's system prompt from conf/prompts.yml.

The grounding contract lives in YAML, not Python. The {unavailable} placeholder is
filled from registry.UNAVAILABLE so the list of layers we knowingly lack is defined
in one place (the registry). The route and finalize nodes share this prompt.
"""
import yaml

from ..config import get_settings
from .geo import registry


def system_prompt():
    """Load and return the formatted system prompt from conf/prompts.yml.

    The {unavailable} placeholder is filled with the comma-separated keys from
    registry.UNAVAILABLE so the model knows which layers it must refuse to use.

    Returns:
        str: the fully formatted system prompt string
    """
    with open(get_settings().prompts_path) as f:
        prompts = yaml.safe_load(f)
    return prompts["system"].format(unavailable=", ".join(registry.UNAVAILABLE)).strip()
