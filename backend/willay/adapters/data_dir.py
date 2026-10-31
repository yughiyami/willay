"""Location of the open datasets (WILLAY_DATA_DIR, default ../willay-data next to the repo).

The data folder is read-only for this project except for `derived/`, where the offline scripts
write their outputs. Raw and referential data are never copied into the repository.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT.parent / "willay-data"
ENV_VAR = "WILLAY_DATA_DIR"


class DataDirError(RuntimeError):
    pass


def resolve_data_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = environ.get(ENV_VAR)
    if not configured:
        return DEFAULT_DATA_DIR.resolve()
    path = Path(configured)
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


class DataDir:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def processed(self, *parts: str) -> Path:
        return self.root.joinpath("processed", *parts)

    def raw(self, *parts: str) -> Path:
        return self.root.joinpath("raw", *parts)

    def derived(self, *parts: str) -> Path:
        return self.root.joinpath("derived", *parts)

    def require(self, *paths: Path) -> None:
        missing = [p for p in paths if not p.exists()]
        if missing:
            listing = "\n  ".join(str(p) for p in missing)
            raise DataDirError(
                f"missing data files under {self.root} (set {ENV_VAR} to the willay-data folder; "
                f"derived files are produced by the scripts in scripts/):\n  {listing}"
            )

    def write_derived(self, name: str, content: str) -> Path:
        """Write a derived output. Anything outside derived/ is refused."""
        base = self.derived().resolve()
        target = (base / name).resolve()
        if base != target.parent and base not in target.parents:
            raise DataDirError(f"refusing to write outside {base}: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target
