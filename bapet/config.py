"""Paths, persistent settings and logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "BlueArchiveDesktopPet"
APP_DISPLAY_NAME = "Blue Archive Desktop Pet"
APP_SLUG = "bluearchive-desktop-pet"

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- paths


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_SLUG


def data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_SLUG


def default_models_dir() -> Path:
    return data_dir() / "models"


def log_dir() -> Path:
    return data_dir() / "logs"


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (works from source and from a PyInstaller bundle)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath("bapet", *parts)


# --------------------------------------------------------------------------- settings

MOVEMENT_MODES = ("float", "walk", "still")


@dataclass
class PetState:
    model: str
    x: float = -1.0
    y: float = -1.0
    size: float = 1.0
    still: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class Settings:
    size: float = 1.0  # global size multiplier
    movement: str = "float"
    speed: float = 1.0
    fps: int = 30
    pitch_deg: float = 12.0
    outline: bool = True
    outline_width: float = 1.5
    always_on_top: bool = True
    launch_at_login: bool = False
    models_dir: str = ""
    first_run: bool = True
    pets: list[PetState] = field(default_factory=list)

    def sanitize(self) -> None:
        self.size = min(max(float(self.size), 0.3), 4.0)
        if self.movement not in MOVEMENT_MODES:
            self.movement = "float"
        self.speed = min(max(float(self.speed), 0.2), 4.0)
        self.fps = int(min(max(int(self.fps), 10), 120))
        self.pitch_deg = min(max(float(self.pitch_deg), -10.0), 45.0)
        self.outline_width = min(max(float(self.outline_width), 0.0), 6.0)
        for p in self.pets:
            p.size = min(max(float(p.size), 0.3), 4.0)

    @property
    def models_path(self) -> Path:
        return Path(self.models_dir) if self.models_dir else default_models_dir()

    # ---- persistence -------------------------------------------------------
    @staticmethod
    def path() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        path = cls.path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
            known = {f.name for f in fields(cls)}
            values = {k: v for k, v in raw.items() if k in known and k != "pets"}
            settings = cls(**values)
            pet_fields = {f.name for f in fields(PetState)}
            for p in raw.get("pets", []):
                if isinstance(p, dict) and isinstance(p.get("model"), str):
                    settings.pets.append(PetState(**{k: v for k, v in p.items() if k in pet_fields}))
            settings.sanitize()
            return settings
        except Exception:  # noqa: BLE001 - corrupt file: keep a backup and start fresh
            log.exception("settings file is corrupt, starting with defaults")
            try:
                path.replace(path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return cls()

    def save(self) -> None:
        path = self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
            tmp.replace(path)
        except OSError:
            log.exception("could not save settings")


# --------------------------------------------------------------------------- logging


def setup_logging(debug: bool = False) -> Path:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logfile = directory / "bapet.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        root.addHandler(sh)
    return logfile
