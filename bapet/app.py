"""Application entry point: tray icon, pet management, single instance, main loop."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import random
import signal
import sys
import threading
import time
import traceback
from dataclasses import fields
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, autostart
from .catalog import Catalog, fetch_catalog
from .config import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_SLUG,
    PetState,
    Settings,
    data_dir,
    resource_path,
    setup_logging,
)
from .dialogs import ModelBrowser, SettingsDialog, show_about
from .models import Downloader, ModelManager
from .pet import Pet
from .renderer import Renderer, RendererError

log = logging.getLogger(__name__)

DEFAULT_PET = "Arisu.glb"
MAX_PETS = 30
SIZES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


def load_catalog() -> Catalog:
    """User-refreshed catalog if present and valid, else the bundled snapshot."""
    bundled = Catalog.load(resource_path("catalog.json"))
    user = data_dir() / "catalog.json"
    if user.exists():
        try:
            cat = Catalog.load(user)
            if cat.generated >= bundled.generated:
                return cat
        except Exception:  # noqa: BLE001
            log.warning("ignoring invalid user catalog %s", user)
    return bundled


class Controller(QObject):
    catalog_changed = Signal()
    _catalog_result = Signal(object, str, object)  # Catalog | None, error, callback

    def __init__(self, app: QApplication, settings: Settings, catalog: Catalog, renderer: Renderer) -> None:
        super().__init__()
        self.app = app
        self.settings = settings
        self.catalog = catalog
        self.renderer = renderer
        self.icon = QIcon(str(resource_path("assets", "icon.png")))
        self.paused = False
        self.hidden = False
        self.pets: list[Pet] = []
        self._dialogs: list = []

        self.downloader = Downloader(settings.models_path)
        self.manager = ModelManager(renderer, self.downloader, catalog)
        self.manager.ready.connect(self._on_model_ready)
        self.manager.error.connect(self._on_model_error)
        self.manager.status.connect(self._on_model_status)
        self._catalog_result.connect(self._on_catalog_result)

        self._save_timer = QTimer(self, singleShot=True, interval=1500)
        self._save_timer.timeout.connect(self.save)
        self._autosave = QTimer(self, interval=60_000)
        self._autosave.timeout.connect(self.save)
        self._autosave.start()

        self._last_tick = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / settings.fps))

        self.tray: QSystemTrayIcon | None = None
        self._build_tray()
        app.screenRemoved.connect(lambda *_: QTimer.singleShot(200, self._rescue_offscreen))

    # ------------------------------------------------------------------ tray / menus
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("system tray not available; use the pet context menu")
            return
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu()
        menu.aboutToShow.connect(lambda: self._fill_global_menu(menu))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.hidden:
                self.set_hidden(False)
            else:
                self.open_browser()

    def _fill_global_menu(self, menu: QMenu) -> None:
        menu.clear()
        menu.addAction("Add Pet…", self.open_browser)
        menu.addAction("Add Random Pet", lambda: self.add_pet(random.choice(self.catalog.skins).file))
        menu.addSeparator()
        pause = menu.addAction("Pause Movement")
        pause.setCheckable(True)
        pause.setChecked(self.paused)
        pause.toggled.connect(self.set_paused)
        hide = menu.addAction("Hide Pets")
        hide.setCheckable(True)
        hide.setChecked(self.hidden)
        hide.toggled.connect(self.set_hidden)
        menu.addSeparator()
        menu.addAction("Model Library…", lambda: self.open_browser(title="Model Library"))
        menu.addAction("Settings…", self.open_settings)
        menu.addAction(f"About {APP_DISPLAY_NAME}", lambda: show_about(None, self.icon))
        menu.addSeparator()
        remove_all = menu.addAction("Remove All Pets", self.remove_all)
        remove_all.setEnabled(bool(self.pets))
        menu.addAction("Quit", self.quit)

    def show_pet_menu(self, pet: Pet, pos: QPoint) -> None:
        menu = QMenu()
        title = menu.addAction(pet.label)
        title.setEnabled(False)
        menu.addSeparator()
        if pet.error:
            menu.addAction("Retry Loading", lambda: (self.manager.retry(pet.state.model),
                                                      setattr(pet, "error", None)))
        menu.addAction("Change Character…", lambda: self.open_browser(pet=pet))

        skin = self.catalog.skin(pet.state.model)
        character = self.catalog.character(skin.character) if skin else None
        if character and len(character.skins) > 1:
            outfits = menu.addMenu("Outfit")
            group = QActionGroup(outfits)
            for s in character.skins:
                act = outfits.addAction(s.name)
                act.setCheckable(True)
                act.setChecked(s.file == pet.state.model)
                group.addAction(act)
                act.triggered.connect(lambda _=False, f=s.file: self.change_model(pet, f))

        if pet.lm is not None and pet.lm.model.animations:
            anims = menu.addMenu("Play Animation")
            for i, name in enumerate(pet.lm.model.animation_names):
                anims.addAction(name.replace("_", " "), lambda i=i: pet.play_named(i))

        sizes = menu.addMenu("Size")
        group = QActionGroup(sizes)
        for factor in SIZES:
            act = sizes.addAction(f"{int(factor * 100)}%")
            act.setCheckable(True)
            act.setChecked(abs(pet.state.size - factor) < 1e-3)
            group.addAction(act)
            act.triggered.connect(lambda _=False, f=factor: self._set_pet_size(pet, f))

        still = menu.addAction("Stay Here")
        still.setCheckable(True)
        still.setChecked(pet.state.still)
        still.toggled.connect(lambda v: (setattr(pet.state, "still", v), self.schedule_save()))
        menu.addAction("Duplicate", lambda: self.add_pet(pet.state.model, near=pet))
        menu.addAction("Remove", lambda: self.remove_pet(pet))
        menu.addSeparator()
        glob = menu.addMenu("Desktop Pet")
        self._fill_global_menu(glob)
        menu.exec(pos)

    # ------------------------------------------------------------------ pets
    def add_pet(self, file: str, near: Pet | None = None, state: PetState | None = None) -> Pet | None:
        if self.catalog.skin(file) is None:
            log.warning("unknown model %s", file)
            return None
        if len(self.pets) >= MAX_PETS:
            self.notify(f"You already have {MAX_PETS} pets — remove one first.")
            return None
        if state is None:
            state = PetState(model=file)
            if near is not None:
                state.x, state.y = near.anchor.x() + 80, near.anchor.y()
                state.size = near.state.size
        pet = Pet(self, state)
        self.pets.append(pet)
        if state not in self.settings.pets:
            self.settings.pets.append(state)
        pet.set_model(file)
        if not self.hidden:
            pet.show()
        self.schedule_save()
        return pet

    def change_model(self, pet: Pet, file: str) -> None:
        if pet in self.pets and file != pet.state.model:
            pet.set_model(file)
            self.schedule_save()

    def remove_pet(self, pet: Pet) -> None:
        if pet not in self.pets:
            return
        self.pets.remove(pet)
        if pet.state in self.settings.pets:
            self.settings.pets.remove(pet.state)
        pet.release_model()
        pet.close()
        self.schedule_save()

    def remove_all(self) -> None:
        for pet in list(self.pets):
            self.remove_pet(pet)

    def _set_pet_size(self, pet: Pet, factor: float) -> None:
        pet.state.size = factor
        self.schedule_save()

    def _on_model_ready(self, file: str) -> None:
        lm = self.manager.get(file)
        for pet in self.pets:
            if pet.state.model == file and pet.lm is None and lm is not None:
                pet.on_model_ready(lm)

    def _on_model_error(self, file: str, message: str) -> None:
        affected = [p for p in self.pets if p.state.model == file and p.lm is None]
        for pet in affected:
            pet.on_model_error(message)
        if affected:
            self.notify(f"{affected[0].label}: {message}", error=True)

    def _on_model_status(self, file: str, text: str) -> None:
        for pet in self.pets:
            if pet.state.model == file:
                pet.on_model_status(text)

    def _rescue_offscreen(self) -> None:
        for pet in self.pets:
            if QApplication.screenAt(pet.anchor.toPoint()) is None:
                area = QApplication.primaryScreen().availableGeometry()
                pet.anchor = QPointF(area.center().x(), area.bottom())
                pet.mode = "idle" if pet.lm else pet.mode

    # ------------------------------------------------------------------ main loop
    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(max(now - self._last_tick, 0.0), 0.1)
        self._last_tick = now
        if self.hidden:
            return
        for pet in list(self.pets):
            try:
                pet.tick(dt)
            except Exception:  # noqa: BLE001 - never let one pet kill the loop
                log.exception("pet tick failed (%s)", pet.state.model)

    # ------------------------------------------------------------------ dialogs
    def _keep(self, dialog) -> None:
        self._dialogs.append(dialog)
        dialog.finished.connect(lambda *_: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_browser(self, pet: Pet | None = None, title: str | None = None) -> None:
        if pet is not None:
            dlg = ModelBrowser(self, title="Change Character", action="Select", current=pet.state.model)
            dlg.chosen.connect(lambda f: self.change_model(pet, f))
        else:
            dlg = ModelBrowser(self, title=title or "Add Pet", action="Add Pet")
            dlg.chosen.connect(self.add_pet)
        self._keep(dlg)

    def open_settings(self) -> None:
        for d in self._dialogs:
            if isinstance(d, SettingsDialog):
                d.raise_()
                d.activateWindow()
                return
        self._keep(SettingsDialog(self))

    def apply_settings(self, **values) -> None:
        s = self.settings
        old_top, old_dir, old_login = s.always_on_top, s.models_path, s.launch_at_login
        known = {f.name for f in fields(Settings)}
        for k, v in values.items():
            if k in known:
                setattr(s, k, v)
        s.sanitize()
        self.timer.setInterval(int(1000 / s.fps))
        if s.models_path != old_dir:
            self.downloader.set_models_dir(s.models_path)
        if s.launch_at_login != old_login and not autostart.set_enabled(s.launch_at_login):
            s.launch_at_login = old_login
            QMessageBox.warning(None, APP_DISPLAY_NAME, "Could not change the start-at-login setting.")
        if s.always_on_top != old_top:
            for pet in self.pets:
                pet.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, s.always_on_top)
                if not self.hidden:
                    pet.show()
        for pet in self.pets:
            pet._frame_key = None
        self.save()

    def clear_model_cache(self) -> int:
        freed = 0
        self.downloader.cancel()
        directory = self.settings.models_path
        if directory.exists():
            for p in list(directory.glob("*.glb")) + list(directory.glob("*.part")):
                try:
                    freed += p.stat().st_size
                    p.unlink()
                except OSError:
                    log.warning("could not delete %s", p)
        return freed

    def refresh_catalog(self, callback: Callable[[bool, str], None]) -> None:
        def work() -> None:
            try:
                self._catalog_result.emit(fetch_catalog(), "", callback)
            except Exception as exc:  # noqa: BLE001
                log.warning("catalog refresh failed: %s", exc)
                self._catalog_result.emit(None, str(exc), callback)

        threading.Thread(target=work, name="catalog", daemon=True).start()

    def _on_catalog_result(self, catalog: Catalog | None, error: str, callback) -> None:
        if catalog is not None:
            # Keep entries for models that pets still use even if they vanished from the wiki.
            used = {p.state.model for p in self.pets} - {s.file for s in catalog.skins}
            if used:
                kept = [c for c in self.catalog.characters if any(s.file in used for s in c.skins)]
                catalog = Catalog(catalog.characters + kept, catalog.generated)
            self.catalog = catalog
            self.manager.catalog = catalog
            try:
                catalog.save(data_dir() / "catalog.json")
            except OSError:
                log.exception("could not save refreshed catalog")
            self.catalog_changed.emit()
            self.notify(f"Model list updated: {len(catalog)} characters, {len(catalog.skins)} models.")
        if callback:
            callback(catalog is not None, error)

    # ------------------------------------------------------------------ state
    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def set_hidden(self, hidden: bool) -> None:
        self.hidden = hidden
        for pet in self.pets:
            pet.setVisible(not hidden)
        if not hidden:
            self._last_tick = time.monotonic()

    def notify(self, message: str, error: bool = False) -> None:
        log.info("notify: %s", message)
        if self.tray is not None and self.tray.supportsMessages():
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(APP_DISPLAY_NAME, message, icon, 5000)

    def schedule_save(self) -> None:
        self._save_timer.start()

    def save(self) -> None:
        for pet in self.pets:
            pet.snapshot_position()
        self.settings.save()

    def restore(self) -> None:
        states = list(self.settings.pets)
        self.settings.pets.clear()
        for st in states:
            if self.catalog.skin(st.model) is None:
                log.warning("dropping pet with unknown model %s", st.model)
                continue
            self.add_pet(st.model, state=st)
        if not self.pets and self.settings.first_run:
            first = DEFAULT_PET if self.catalog.skin(DEFAULT_PET) else self.catalog.skins[0].file
            self.add_pet(first)
            self.settings.first_run = False
            self.save()
            QTimer.singleShot(1500, lambda: self.notify(
                "Drag your pet to move it, click it to say hi, right-click for options. "
                "Add more students from the tray icon."))
        elif not self.pets and self.tray is None:
            self.open_browser()

    def quit(self) -> None:
        self.save()
        self.timer.stop()
        self.downloader.shutdown()
        for pet in list(self.pets):
            pet.release_model()
            pet.close()
        self.pets.clear()
        self.manager.shutdown()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


# --------------------------------------------------------------------------- single instance


def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{APP_SLUG}-{hashlib.sha1(user.encode()).hexdigest()[:10]}"


def _notify_running_instance(message: str) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(message.encode())
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _listen(controller: Controller) -> QLocalServer:
    server = QLocalServer(controller)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(_server_name()):
        QLocalServer.removeServer(_server_name())  # stale socket from a crash
        server.listen(_server_name())

    def on_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read() -> None:
            msg = bytes(conn.readAll()).decode(errors="ignore").strip()
            if msg.startswith("add:") and controller.catalog.skin(msg[4:]):
                controller.add_pet(msg[4:])
            else:
                controller.set_hidden(False)
                controller.open_browser()

        conn.readyRead.connect(read)
        conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(on_connection)
    return server


# --------------------------------------------------------------------------- main


def _install_excepthook() -> None:
    shown = {"done": False}

    def hook(exc_type, exc, tb) -> None:
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        if not shown["done"] and QApplication.instance() is not None:
            shown["done"] = True
            QMessageBox.critical(
                None, APP_DISPLAY_NAME,
                f"An unexpected error occurred:\n\n{exc}\n\nDetails were written to the log file.\n\n"
                + "".join(traceback.format_exception_only(exc_type, exc)).strip(),
            )

    sys.excepthook = hook
    threading.excepthook = lambda args: log.critical(
        "unhandled exception in thread %s", args.thread, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bapet", description=APP_DISPLAY_NAME)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--add", metavar="MODEL", help="add a pet, e.g. 'Arisu.glb' or 'Hina'")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--reset", action="store_true", help="reset settings and remove all pets")
    return p.parse_known_args(argv)[0]


def _resolve_model(catalog: Catalog, name: str | None) -> str | None:
    if not name:
        return None
    if catalog.skin(name):
        return name
    lowered = name.lower()
    for s in catalog.skins:
        if s.label.lower() == lowered or s.file.lower() == f"{lowered}.glb":
            return s.file
    ch = next((c for c in catalog.characters if c.name.lower() == lowered), None)
    return ch.skins[0].file if ch else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logfile = setup_logging(args.debug)
    log.info("%s %s starting (Python %s, %s)", APP_DISPLAY_NAME, __version__, sys.version.split()[0], sys.platform)

    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        # Wayland forbids clients from positioning their own windows; use XWayland.
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_DISPLAY_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setDesktopFileName(APP_SLUG)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(str(resource_path("assets", "icon.png"))))
    _install_excepthook()

    try:
        catalog = load_catalog()
    except Exception as exc:  # noqa: BLE001
        log.exception("catalog missing")
        QMessageBox.critical(None, APP_DISPLAY_NAME, f"The model catalog could not be loaded:\n{exc}")
        return 1

    model_arg = _resolve_model(catalog, args.add)
    if args.add and not model_arg:
        print(f"Unknown model: {args.add}", file=sys.stderr)
    if _notify_running_instance(f"add:{model_arg}" if model_arg else "show"):
        log.info("another instance is running; handed over")
        return 0

    settings = Settings() if args.reset else Settings.load()
    try:
        renderer = Renderer()
    except (RendererError, Exception) as exc:  # noqa: BLE001
        log.exception("renderer init failed")
        QMessageBox.critical(
            None, APP_DISPLAY_NAME,
            "OpenGL 3.3 is required but could not be initialised.\n"
            "Please update your graphics drivers.\n\n"
            f"{exc}\n\nLog: {logfile}",
        )
        return 2

    controller = Controller(app, settings, catalog, renderer)
    server = _listen(controller)
    controller.restore()
    if model_arg:
        controller.add_pet(model_arg)
    app.aboutToQuit.connect(controller.save)

    # Quit cleanly (saving pet positions) on Ctrl+C / logout / kill. Python only runs
    # signal handlers between bytecodes, so wake the interpreter periodically.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, controller.quit))
    wake = QTimer(interval=250)
    wake.timeout.connect(lambda: None)
    wake.start()

    code = app.exec()
    server.close()
    log.info("exit %s", code)
    return code
