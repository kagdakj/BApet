"""Model catalog: every character/skin listed on https://bluearchive.wiki/wiki/Models.

A snapshot is bundled with the app (catalog.json); users can refresh it at
runtime, which re-scrapes the wiki and the GitHub tree that hosts the files.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

WIKI_URL = "https://bluearchive.wiki/wiki/Models"
REPO = "lihaohong6/BlueArchiveModels"
BRANCH = "main"
TREE_API = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}"
MIRRORS = (
    f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{{file}}",
    f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{{file}}",
)
USER_AGENT = "BlueArchiveDesktopPet/1.0 (+https://github.com/)"
CATALOG_VERSION = 1


@dataclass(frozen=True)
class Skin:
    character: str
    name: str
    file: str
    size: int = 0
    sha: str = ""  # git blob sha1, used to verify downloads

    @property
    def key(self) -> str:
        return self.file

    @property
    def label(self) -> str:
        return self.character if self.name == "Default" else f"{self.character} ({self.name})"

    def urls(self) -> list[str]:
        quoted = urllib.parse.quote(self.file)
        return [m.format(file=quoted) for m in MIRRORS]


@dataclass(frozen=True)
class Character:
    name: str
    skins: tuple[Skin, ...]


class Catalog:
    def __init__(self, characters: list[Character], generated: str = "") -> None:
        self.characters = sorted(characters, key=lambda c: c.name.lower())
        self.generated = generated
        self._by_file = {s.file: s for c in self.characters for s in c.skins}

    def __len__(self) -> int:
        return len(self.characters)

    @property
    def skins(self) -> list[Skin]:
        return [s for c in self.characters for s in c.skins]

    def skin(self, file: str) -> Skin | None:
        return self._by_file.get(file)

    def character(self, name: str) -> Character | None:
        for c in self.characters:
            if c.name == name:
                return c
        return None

    # ---- (de)serialization -------------------------------------------------
    def to_json(self) -> dict:
        return {
            "version": CATALOG_VERSION,
            "generated": self.generated,
            "source": WIKI_URL,
            "characters": [
                {
                    "name": c.name,
                    "skins": [{"name": s.name, "file": s.file, "size": s.size, "sha": s.sha} for s in c.skins],
                }
                for c in self.characters
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "Catalog":
        if data.get("version") != CATALOG_VERSION:
            raise ValueError("unsupported catalog version")
        chars = []
        for c in data["characters"]:
            skins = tuple(
                Skin(c["name"], s["name"], s["file"], int(s.get("size", 0)), s.get("sha", "")) for s in c["skins"]
            )
            if skins:
                chars.append(Character(c["name"], skins))
        if not chars:
            raise ValueError("empty catalog")
        return cls(chars, data.get("generated", ""))

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls.from_json(json.loads(path.read_text("utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- scraping


def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


_HEADING_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S)
_VIEWER_RE = re.compile(r'<div class="model-viewer"[^>]*?\sdata-models="([^"]*)"', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_wiki_html(page: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return [(character, [(skin_name, file_name), ...]), ...] from the Models page HTML."""
    out = []
    headings = list(_HEADING_RE.finditer(page))
    for i, h in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(page)
        viewer = _VIEWER_RE.search(page, h.end(), end)
        if not viewer:
            continue
        name = html.unescape(_TAG_RE.sub("", h.group(1))).strip()
        try:
            entries = json.loads(html.unescape(viewer.group(1)))
        except json.JSONDecodeError:
            log.warning("could not parse model list for %s", name)
            continue
        skins = []
        for e in entries:
            src = e.get("src", "")
            file = urllib.parse.unquote(src.rsplit("/", 1)[-1])
            if file.lower().endswith((".glb", ".gltf")):
                skins.append((e.get("name") or "Default", file))
        if name and skins:
            out.append((name, skins))
    return out


def fetch_catalog() -> Catalog:
    """Scrape the wiki + GitHub tree and build a fresh catalog (network)."""
    page = _get(WIKI_URL).decode("utf-8", "replace")
    sections = parse_wiki_html(page)
    if not sections:
        raise RuntimeError("no models found on the wiki page (layout changed?)")
    meta: dict[str, tuple[int, str]] = {}
    try:
        tree = json.loads(_get(TREE_API))
        for entry in tree.get("tree", []):
            if entry.get("type") == "blob":
                meta[entry["path"]] = (int(entry.get("size", 0)), entry.get("sha", ""))
    except Exception as exc:  # noqa: BLE001 - sizes/hashes are optional
        log.warning("GitHub tree unavailable, catalog will lack sizes/hashes: %s", exc)
    chars = []
    for name, skins in sections:
        chars.append(
            Character(
                name,
                tuple(Skin(name, sname, f, *meta.get(f, (0, ""))) for sname, f in skins),
            )
        )
    return Catalog(chars, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
