"""Import-path and environment bootstrap for the API server."""

from __future__ import annotations

import sys
import os
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVICE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _mask_key(value: str) -> str:
    return f"len={len(value)} tail={value[-4:] if value else ''}"


def _drop_shadowed_openai_key(env_path: Path, parser) -> None:
    file_key = str((parser(env_path).get("OPENAI_API_KEY") or "")).strip()
    process_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if file_key and process_key and file_key != process_key:
        print(
            "WARN: OPENAI_API_KEY in the process environment differs from the project .env; "
            f"using project .env instead. env_path={env_path} "
            f"process={_mask_key(process_key)} file={_mask_key(file_key)}",
            flush=True,
        )
        os.environ.pop("OPENAI_API_KEY", None)


try:
    from dotenv import dotenv_values, load_dotenv

    root_env_path = _REPO_ROOT.parent / ".env"
    root_evn_path = _REPO_ROOT.parent / ".evn"
    local_env_path = _SERVICE_DIR / ".env"
    if root_env_path.exists():
        _drop_shadowed_openai_key(root_env_path, dotenv_values)
        load_dotenv(root_env_path, override=False)
    elif root_evn_path.exists():
        _drop_shadowed_openai_key(root_evn_path, dotenv_values)
        load_dotenv(root_evn_path, override=False)
    if local_env_path.exists():
        load_dotenv(local_env_path, override=False)
except ImportError:
    pass
