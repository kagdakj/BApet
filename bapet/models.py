"""Model downloading (with integrity checks) and loading (parse in worker, upload on GUI thread)."""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from . import gltf
from .catalog import USER_AGENT, Catalog, Skin
from .renderer import GpuModel, Renderer, decode_textures

log = logging.getLogger(__name__)

CHUNK = 256 * 1024

# Animation roles, in priority order. Cafe animations exist for almost every model;
# battle-only variants fall back to their combat equivalents.
IDLE_ANIMS = ("Cafe_Idle", "Formation_Idle", "Normal_Idle", "Stand_Idle", "Idle")
WALK_ANIMS = ("Cafe_Walk", "Move_Ing", "Walk")
REACT_ANIMS = ("Cafe_Reaction", "Normal_Callsign", "Victory_Start", "Public01")
PICKUP_ANIMS = ("Formation_Pickup", "Vital_Panic", "Cafe_Reaction")


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


def glb_complete(path: Path) -> bool:
    """Cheap integrity check: GLB header length matches the file size."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(12)
        if len(head) < 12:
            return False
        magic, _version, length = struct.unpack("<4sII", head)
        return magic == b"glTF" and length == size
    except OSError:
        return False


class Downloader(QObject):
    """Downloads model files from the CDN mirrors into the models directory."""

    progress = Signal(str, int, int)  # file, received, total
    finished = Signal(str, str)  # file, local path
    failed = Signal(str, str)  # file, error message

    def __init__(self, models_dir: Path, workers: int = 3) -> None:
        super().__init__()
        self.models_dir = models_dir
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download")
        self._active: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._cancel_files: set[str] = set()

    def set_models_dir(self, path: Path) -> None:
        self.models_dir = path

    def path_for(self, skin: Skin) -> Path:
        return self.models_dir / skin.file

    def is_cached(self, skin: Skin) -> bool:
        path = self.path_for(skin)
        if not path.exists():
            return False
        if skin.size and path.stat().st_size != skin.size:
            return glb_complete(path)  # catalog may be older than the file
        return glb_complete(path)

    def is_active(self, file: str) -> bool:
        with self._lock:
            return file in self._active

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def request(self, skin: Skin) -> None:
        """Start downloading `skin` unless cached or already in progress."""
        if self.is_cached(skin):
            self.finished.emit(skin.file, str(self.path_for(skin)))
            return
        with self._lock:
            if skin.file in self._active:
                return
            self._cancel_files.discard(skin.file)
            self._active[skin.file] = self._pool.submit(self._run, skin)

    def cancel(self, file: str | None = None) -> None:
        with self._lock:
            if file is None:
                self._cancel_files.update(self._active)
            else:
                self._cancel_files.add(file)

    def shutdown(self) -> None:
        self._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- worker ------------------------------------------------------------
    def _run(self, skin: Skin) -> None:
        try:
            path = self._download(skin)
            self.finished.emit(skin.file, str(path))
        except Cancelled:
            self.failed.emit(skin.file, "cancelled")
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.warning("download of %s failed: %s", skin.file, exc)
            self.failed.emit(skin.file, str(exc))
        finally:
            with self._lock:
                self._active.pop(skin.file, None)

    def _check_cancel(self, file: str) -> None:
        if self._cancel.is_set() or file in self._cancel_files:
            raise Cancelled()

    def _download(self, skin: Skin) -> Path:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        dest = self.path_for(skin)
        tmp = dest.with_name(dest.name + ".part")
        unverified = dest.with_name(dest.name + ".unverified")
        errors = []
        urls = skin.urls()
        try:
            for attempt, url in enumerate(urls * 2):  # two rounds over all mirrors
                self._check_cancel(skin.file)
                if attempt == len(urls):
                    time.sleep(2.0)
                try:
                    sha_ok = self._fetch(url, tmp, skin)
                except (urllib.error.URLError, OSError, DownloadError) as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                if not glb_complete(tmp):
                    errors.append(f"{url}: incomplete or invalid GLB")
                    continue
                if not sha_ok:
                    # Keep it as a fallback: the catalog may be older than the repository.
                    errors.append(f"{url}: checksum mismatch")
                    os.replace(tmp, unverified)
                    if attempt + 1 < len(urls):
                        continue
                    log.warning("%s: checksum differs from catalog, using downloaded file", skin.file)
                    os.replace(unverified, dest)
                    return dest
                os.replace(tmp, dest)
                log.info("downloaded %s from %s (%d bytes)", skin.file, url, dest.stat().st_size)
                return dest
            if unverified.exists():
                os.replace(unverified, dest)
                return dest
            raise DownloadError("all mirrors failed:\n" + "\n".join(errors[-4:]))
        finally:
            tmp.unlink(missing_ok=True)
            unverified.unlink(missing_ok=True)

    def _fetch(self, url: str, tmp: Path, skin: Skin) -> bool:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or skin.size or 0)
            hasher = hashlib.sha1()
            if total:
                hasher.update(b"blob %d\0" % total)
            received = 0
            last_emit = 0.0
            with tmp.open("wb") as fh:
                while True:
                    self._check_cancel(skin.file)
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_emit > 0.1:
                        last_emit = now
                        self.progress.emit(skin.file, received, total)
            if total and received != total:
                raise DownloadError(f"short read ({received}/{total} bytes)")
            self.progress.emit(skin.file, received, total or received)
        if not skin.sha or not total:
            return True
        return hasher.hexdigest() == skin.sha


# --------------------------------------------------------------------------- loading


def decode_image(data: bytes, mime: str) -> tuple[int, int, bytes] | None:
    img = QImage.fromData(data)
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    return w, h, bytes(img.constBits())[: w * h * 4]


@dataclass
class LoadedModel:
    skin: Skin
    model: gltf.Model
    gpu: GpuModel
    idle: int | None
    walk: int | None
    react: int | None
    pickup: int | None
    bounds: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    refs: int = 0

    def anim_bounds(self, index: int | None) -> tuple[float, float, float]:
        if index is None:
            return self.bounds.get(-1) or self.model.animation_bounds(self.model.rest_pose())
        b = self.bounds.get(index)
        if b is None:
            b = self.model.animation_bounds(self.model.bake(index))
            self.bounds[index] = b
        return b


@dataclass
class _Parsed:
    model: gltf.Model
    textures: list
    roles: dict[str, int | None]
    bounds: dict[int, tuple[float, float, float]]


def _parse(path: str) -> _Parsed:
    model = gltf.load_file(path)
    textures = decode_textures(model, decode_image)
    roles = {
        "idle": model.find_animation(*IDLE_ANIMS),
        "walk": model.find_animation(*WALK_ANIMS),
        "react": model.find_animation(*REACT_ANIMS),
        "pickup": model.find_animation(*PICKUP_ANIMS),
    }
    if roles["idle"] is None and model.animations:
        roles["idle"] = 0
    bounds = {}
    for idx in dict.fromkeys(v for v in roles.values() if v is not None):
        bounds[idx] = model.animation_bounds(model.bake(idx))
    rest = model.rest_pose()
    bounds[-1] = model.animation_bounds(rest)
    return _Parsed(model, textures, roles, bounds)


class ModelManager(QObject):
    """Owns downloaded/parsed/uploaded models; pets acquire and release them by file name."""

    ready = Signal(str)  # file
    error = Signal(str, str)  # file, message
    status = Signal(str, str)  # file, human readable progress

    _parsed = Signal(str, object, str)  # file, _Parsed | None, error (worker -> GUI thread)

    KEEP_UNUSED = 2

    def __init__(self, renderer: Renderer, downloader: Downloader, catalog: Catalog) -> None:
        super().__init__()
        self.renderer = renderer
        self.downloader = downloader
        self.catalog = catalog
        self._loaded: dict[str, LoadedModel] = {}
        self._pending: set[str] = set()
        self._unused: list[str] = []
        self._pending_refs: dict[str, int] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="parse")
        downloader.finished.connect(self._on_downloaded)
        downloader.failed.connect(self._on_download_failed)
        downloader.progress.connect(self._on_progress)
        self._parsed.connect(self._on_parsed)

    def get(self, file: str) -> LoadedModel | None:
        return self._loaded.get(file)

    def acquire(self, file: str) -> LoadedModel | None:
        """Request a model. Returns it if ready; otherwise `ready`/`error` fire later."""
        lm = self._loaded.get(file)
        if lm is not None:
            lm.refs += 1
            if file in self._unused:
                self._unused.remove(file)
            return lm
        self._pending_refs[file] = self._pending_refs.get(file, 0) + 1
        self._start(file)
        return None

    def retry(self, file: str) -> None:
        if file not in self._loaded:
            self._start(file)

    def release(self, file: str) -> None:
        if file in self._pending_refs:
            self._pending_refs[file] -= 1
            if self._pending_refs[file] <= 0:
                del self._pending_refs[file]
            return
        lm = self._loaded.get(file)
        if lm is None:
            return
        lm.refs -= 1
        if lm.refs <= 0:
            lm.refs = 0
            self._unused.append(file)
            while len(self._unused) > self.KEEP_UNUSED:
                old = self._unused.pop(0)
                victim = self._loaded.pop(old, None)
                if victim is not None:
                    victim.gpu.release()
                    log.info("unloaded %s", old)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        for lm in self._loaded.values():
            lm.gpu.release()
        self._loaded.clear()

    # ---- internals ---------------------------------------------------------
    def _start(self, file: str) -> None:
        if file in self._pending:
            return
        skin = self.catalog.skin(file)
        if skin is None:
            self.error.emit(file, "Model is not in the catalog")
            return
        self._pending.add(file)
        if self.downloader.is_cached(skin):
            self._load(file, str(self.downloader.path_for(skin)))
        else:
            self.status.emit(file, "Downloading…")
            self.downloader.request(skin)

    def _on_progress(self, file: str, received: int, total: int) -> None:
        if file in self._pending:
            pct = f" {received * 100 // total}%" if total else f" {received // 1_000_000} MB"
            self.status.emit(file, "Downloading" + pct)

    def _on_download_failed(self, file: str, message: str) -> None:
        if file in self._pending:
            self._pending.discard(file)
            self.error.emit(file, f"Download failed: {message}")

    def _on_downloaded(self, file: str, path: str) -> None:
        if file in self._pending and file not in self._loaded:
            self._load(file, path)

    def _load(self, file: str, path: str) -> None:
        self.status.emit(file, "Loading…")

        def work() -> None:
            try:
                parsed = _parse(path)
                self._parsed.emit(file, parsed, "")
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to load %s", path)
                if isinstance(exc, gltf.GLTFError):
                    # Corrupt cache entry: delete so the next attempt re-downloads it.
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self._parsed.emit(file, None, f"Could not load model: {exc}")

        self._pool.submit(work)

    def _on_parsed(self, file: str, parsed: _Parsed | None, err: str) -> None:
        self._pending.discard(file)
        if parsed is None:
            self.error.emit(file, err)
            return
        refs = self._pending_refs.pop(file, 0)
        if refs <= 0:
            return  # nobody wants it anymore
        skin = self.catalog.skin(file)
        try:
            gpu = self.renderer.upload(parsed.model, parsed.textures)
        except Exception as exc:  # noqa: BLE001
            log.exception("GPU upload failed for %s", file)
            self.error.emit(file, f"GPU upload failed: {exc}")
            return
        r = parsed.roles
        self._loaded[file] = LoadedModel(
            skin, parsed.model, gpu, r["idle"], r["walk"], r["react"], r["pickup"], parsed.bounds, refs
        )
        log.info("loaded %s (%d animations)", file, len(parsed.model.animations))
        self.ready.emit(file)
