"""Regenerate bapet/catalog.json from https://bluearchive.wiki/wiki/Models."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bapet.catalog import fetch_catalog  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    catalog = fetch_catalog()
    out = ROOT / "bapet" / "catalog.json"
    catalog.save(out)
    skins = catalog.skins
    missing = [s.file for s in skins if not s.sha]
    print(f"{len(catalog)} characters, {len(skins)} models, {sum(s.size for s in skins) / 1e9:.2f} GB -> {out}")
    if missing:
        print(f"warning: {len(missing)} models not found in the GitHub repo: {missing[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
