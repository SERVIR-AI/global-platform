"""Load the agent's system prompt from conf/prompts.yml.

The grounding contract lives in YAML, not Python. The {unavailable} placeholder is
filled from registry.UNAVAILABLE so the list of layers we knowingly lack is defined
in one place (the registry). The route and finalize nodes share this prompt.
"""
import yaml

from ..config import get_settings
from .geo import registry


def system_prompt():
    with open(get_settings().prompts_path) as f:
        prompts = yaml.safe_load(f)
    return prompts["system"].format(unavailable=", ".join(registry.UNAVAILABLE)).strip()
