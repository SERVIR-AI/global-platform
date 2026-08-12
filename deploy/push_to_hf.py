#!/usr/bin/env python3
"""Assemble and upload the platform to a Hugging Face Space (Docker SDK).

Uploads through huggingface_hub rather than git, so large files are handled
without a local git-lfs install. Only what the MCP needs is shipped — notably
NOT cache/tiffs (1.8GB of rasters this deployment does not serve).

    HF_TOKEN=hf_... python deploy/push_to_hf.py <owner>/<space-name>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (source, destination) — destination is relative to the Space root. The layout
# mirrors the repo so the Dockerfile's paths resolve unchanged.
TREE: list[tuple[str, str]] = [
    ("Dockerfile", "Dockerfile"),
    ("pyproject.toml", "pyproject.toml"),
    ("uv.lock", "uv.lock"),
    ("apps/api/pyproject.toml", "apps/api/pyproject.toml"),
    ("apps/api/src", "apps/api/src"),
    ("conf", "conf"),
    ("cache/rag/food-security", "cache/rag/food-security"),
    ("deploy/entrypoint.sh", "deploy/entrypoint.sh"),
    ("deploy/litestream.yml", "deploy/litestream.yml"),
    ("deploy/hf-space-README.md", "README.md"),
]

# The web app is built inside the image, so its SOURCE ships (never node_modules).
WEB = ["package.json", "package-lock.json", "index.html", "src", "public",
       "vite.config.ts", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"]

PRUNE = {"__pycache__", ".pytest_cache", "node_modules", ".DS_Store", ".venv"}


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*PRUNE), dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo_id = sys.argv[1]
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("FATAL: set HF_TOKEN (a write-scoped token from "
              "https://huggingface.co/settings/tokens)", file=sys.stderr)
        return 1

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("FATAL: pip install huggingface_hub", file=sys.stderr)
        return 1

    staging = Path(tempfile.mkdtemp(prefix="grp-space-"))
    for src, dst in TREE:
        s = REPO / src
        if not s.exists():
            print(f"FATAL: missing {src}", file=sys.stderr)
            return 1
        _copy(s, staging / dst)
    for item in WEB:
        s = REPO / "apps/web" / item
        if s.exists():
            _copy(s, staging / "apps/web" / item)

    size = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
    print(f"staged {size / 1e6:.0f} MB at {staging}")

    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    api.upload_folder(folder_path=str(staging), repo_id=repo_id, repo_type="space",
                      commit_message="deploy: MCP server + resolver + embed host")
    print(f"\nSpace:  https://huggingface.co/spaces/{repo_id}")
    print(f"URL:    https://{repo_id.replace('/', '-').lower()}.hf.space")
    print("\nSet these as Space secrets (Settings -> Variables and secrets):")
    print("  OPENAI_API_KEY              (embeddings)")
    print("  GRP_API_TOKEN               (gates /mcp; the resolver stays public)")
    print("  GRP_ALLOW_EPHEMERAL_RECEIPTS=1   (acknowledges receipts do not persist here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
