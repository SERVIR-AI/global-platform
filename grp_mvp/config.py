"""LLM endpoint config. Reads a .env file (KEY=VALUE) next to this module, so
nothing needs to be exported in the shell. Switching providers is MODEL + BASE_URL.

  Provider     GRP_MODEL                          GRP_BASE_URL                  key
  OpenAI       gpt-4o                             (unset)                       OPENAI_API_KEY
  OpenRouter   anthropic/claude-opus-4-8          https://openrouter.ai/api/v1  GRP_API_KEY
  Ollama       llama3.1                           http://localhost:11434/v1     (any)
  vLLM         meta-llama/Llama-3.1-8B-Instruct   http://localhost:8000/v1      (any)
"""
import os


def _load_env():
    for path in (os.path.join(os.path.dirname(__file__), ".env"), ".env"):
        if not os.path.exists(path):
            continue
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


_load_env()

MODEL = os.environ.get("GRP_MODEL", "gpt-4o")
BASE_URL = os.environ.get("GRP_BASE_URL") or None
API_KEY = os.environ.get("GRP_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"

# USD per (input, output) token for the cost line; 0 for local models.
PRICE = (float(os.environ.get("GRP_PRICE_IN", "0")) / 1_000_000,
         float(os.environ.get("GRP_PRICE_OUT", "0")) / 1_000_000)
