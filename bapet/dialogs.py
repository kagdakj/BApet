"""Model browser, settings and about dialogs."""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .catalog import WIKI_URL, Catalog, Skin
from .config import APP_DISPLAY_NAME, Settings, default_models_dir, log_dir

if TYPE_CHECKING:
    from .app import Controller

FILE_ROLE = Qt.ItemDataRole.UserRole


def human_size(n: int) -> str:
    if n <= 0:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB"


class ModelBrowser(QDialog):
    """Pick a character/outfit, manage downloads."""

    chosen = Signal(str)

    def __init__(self, controller: "Controller", title: str = "Add Pet", action: str = "Add Pet",
                 current: str | None = None) -> None:
        super().__init__(None)
        self.controller = controller
        self.setWindowTitle(f"{title} — {APP_DISPLAY_NAME}")
        self.setWindowIcon(controller.icon)
        self.resize(560, 640)
        self._items: dict[str, QTreeWidgetItem] = {}
        self._bulk: set[str] = set()
        self._bulk_total = 0

        self.search = QLineEdit(placeholderText="Search characters or outfits…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._accept)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Character / Outfit", "Size", "Status"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda *_: self._accept())
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.btn_random = QPushButton("Random")
        self.btn_random.clicked.connect(self._random)
        self.btn_download = QPushButton("Download")
        self.btn_download.clicked.connect(self._download_selected)
        self.btn_all = QPushButton("Download All")
        self.btn_all.clicked.connect(self._download_all)
        self.btn_cancel = QPushButton("Cancel Downloads")
        self.btn_cancel.clicked.connect(self._cancel_downloads)
        self.btn_refresh = QPushButton("Refresh List")
        self.btn_refresh.setToolTip(f"Re-read the model list from {WIKI_URL}")
        self.btn_refresh.clicked.connect(self._refresh)
        self.btn_ok = QPushButton(action)
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self._accept)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)

        tools = QHBoxLayout()
        for b in (self.btn_download, self.btn_all, self.btn_cancel, self.btn_refresh):
            tools.addWidget(b)
        tools.addStretch()
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_random)
        bottom.addStretch()
        bottom.addWidget(self.btn_ok)
        bottom.addWidget(btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.tree, 1)
        layout.addLayout(tools)
        layout.addWidget(self.progress)
        layout.addWidget(self.summary)
        layout.addLayout(bottom)

        dl = controller.downloader
        dl.progress.connect(self._on_progress)
        dl.finished.connect(self._on_finished)
        dl.failed.connect(self._on_failed)
        controller.catalog_changed.connect(self._populate)

        self._populate()
        if current and current in self._items:
            self.tree.setCurrentItem(self._items[current])
        self.search.setFocus()

    # ---- population --------------------------------------------------------
    @property
    def catalog(self) -> Catalog:
        return self.controller.catalog

    def _populate(self) -> None:
        current = self._selected_file()
        self.tree.clear()
        self._items.clear()
        for ch in self.catalog.characters:
            parent = QTreeWidgetItem([ch.name, "", ""])
            parent.setData(0, FILE_ROLE, ch.skins[0].file)
            total = 0
            for skin in ch.skins:
                child = QTreeWidgetItem([skin.name, human_size(skin.size), ""])
                child.setData(0, FILE_ROLE, skin.file)
                child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                parent.addChild(child)
                self._items[skin.file] = child
                total += skin.size
            parent.setText(1, f"{len(ch.skins)} outfit{'s' if len(ch.skins) != 1 else ''}")
            self.tree.addTopLevelItem(parent)
        self._refresh_status()
        self._filter(self.search.text())
        if current in self._items:
            self.tree.setCurrentItem(self._items[current])

    def _refresh_status(self) -> None:
        dl = self.controller.downloader
        cached = 0
        missing_bytes = 0
        for file, item in self._items.items():
            skin = self.catalog.skin(file)
            if dl.is_cached(skin):
                item.setText(2, "✓ Downloaded")
                cached += 1
            elif dl.is_active(file):
                item.setText(2, "Queued…")
                missing_bytes += skin.size
            else:
                item.setText(2, "")
                missing_bytes += skin.size
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            n = parent.childCount()
            have = sum(1 for k in range(n) if parent.child(k).text(2).startswith("✓"))
            parent.setText(2, "✓" if have == n else (f"{have}/{n}" if have else ""))
        total = len(self._items)
        self.summary.setText(
            f"{len(self.catalog)} characters, {total} models — {cached} downloaded, "
            f"{human_size(missing_bytes) or '0 MB'} left to download. "
            f"Models are downloaded on demand to: {dl.models_dir}"
        )
        self.btn_all.setText(f"Download All ({human_size(missing_bytes)})" if missing_bytes else "All Downloaded")
        self.btn_all.setEnabled(missing_bytes > 0)
        self._update_buttons()

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            name_match = q in parent.text(0).lower()
            any_child = False
            for k in range(parent.childCount()):
                child = parent.child(k)
                match = not q or name_match or q in child.text(0).lower()
                child.setHidden(not match)
                any_child |= match
            parent.setHidden(not any_child)
            parent.setExpanded(bool(q) and any_child)
        cur = self.tree.currentItem()
        if q and (cur is None or cur.isHidden() or (cur.parent() and cur.parent().isHidden())):
            for i in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(i)
                if not parent.isHidden():
                    first = next((parent.child(k) for k in range(parent.childCount())
                                  if not parent.child(k).isHidden()), parent)
                    self.tree.setCurrentItem(first)
                    break

    def _selected_file(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, FILE_ROLE) if item else None

    def _update_buttons(self) -> None:
        file = self._selected_file()
        self.btn_ok.setEnabled(file is not None)
        skin = self.catalog.skin(file) if file else None
        self.btn_download.setEnabled(bool(skin) and not self.controller.downloader.is_cached(skin))
        self.btn_cancel.setEnabled(self.controller.downloader.active_count() > 0)

    # ---- actions -----------------------------------------------------------
    def _accept(self) -> None:
        file = self._selected_file()
        if file:
            self.chosen.emit(file)
            self.accept()

    def _random(self) -> None:
        visible = [f for f, it in self._items.items() if not it.isHidden() and not it.parent().isHidden()]
        if visible:
            self.chosen.emit(random.choice(visible))
            self.accept()

    def _download_selected(self) -> None:
        file = self._selected_file()
        skin = self.catalog.skin(file) if file else None
        if skin:
            self._queue([skin])

    def _download_all(self) -> None:
        missing = [s for s in self.catalog.skins if not self.controller.downloader.is_cached(s)]
        size = sum(s.size for s in missing)
        if not missing:
            return
        answer = QMessageBox.question(
            self, "Download all models",
            f"Download {len(missing)} models ({human_size(size)})?\n\n"
            "Downloads run in the background; you can keep using the app.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._queue(missing)

    def _queue(self, skins: list[Skin]) -> None:
        for s in skins:
            if s.file not in self._bulk:
                self._bulk.add(s.file)
                self._bulk_total += 1
            self.controller.downloader.request(s)
        self._show_progress()
        self._refresh_status()

    def _cancel_downloads(self) -> None:
        self.controller.downloader.cancel()
        self._bulk.clear()
        self._bulk_total = 0
        self._show_progress()

    def _refresh(self) -> None:
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("Refreshing…")
        self.controller.refresh_catalog(self._refresh_done)

    def _refresh_done(self, ok: bool, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("Refresh List")
        if not ok:
            QMessageBox.warning(self, "Refresh failed", message)

    # ---- download feedback -------------------------------------------------
    def _show_progress(self) -> None:
        if not self._bulk_total:
            self.progress.setVisible(False)
            return
        done = self._bulk_total - len(self._bulk)
        self.progress.setVisible(True)
        self.progress.setMaximum(self._bulk_total)
        self.progress.setValue(done)
        self.progress.setFormat(f"Downloaded {done} / {self._bulk_total}")
        if not self._bulk:
            self._bulk_total = 0
            self.progress.setVisible(False)

    def _on_progress(self, file: str, received: int, total: int) -> None:
        item = self._items.get(file)
        if item is not None:
            item.setText(2, f"{received * 100 // total}%" if total else human_size(received))

    def _on_finished(self, file: str, _path: str) -> None:
        item = self._items.get(file)
        if item is not None:
            item.setText(2, "✓ Downloaded")
        self._bulk.discard(file)
        self._show_progress()
        self._refresh_status()

    def _on_failed(self, file: str, message: str) -> None:
        item = self._items.get(file)
        if item is not None:
            item.setText(2, "Cancelled" if message == "cancelled" else "Failed")
            item.setToolTip(2, message)
        self._bulk.discard(file)
        self._show_progress()
        self._update_buttons()

    def done(self, result: int) -> None:  # noqa: D401 - disconnect global signals on close
        dl = self.controller.downloader
        for sig, slot in ((dl.progress, self._on_progress), (dl.finished, self._on_finished),
                          (dl.failed, self._on_failed), (self.controller.catalog_changed, self._populate)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)


class SettingsDialog(QDialog):
    def __init__(self, controller: "Controller") -> None:
        super().__init__(None)
        self.controller = controller
        s = controller.settings
        self.setWindowTitle(f"Settings — {APP_DISPLAY_NAME}")
        self.setWindowIcon(controller.icon)

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(30, 300)
        self.size_slider.setValue(int(s.size * 100))
        self.size_label = QLabel()
        self.size_slider.valueChanged.connect(lambda v: self.size_label.setText(f"{v}%"))
        self.size_slider.valueChanged.emit(self.size_slider.value())
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_slider, 1)
        size_row.addWidget(self.size_label)

        self.movement = QComboBox()
        self.movement.addItem("Float around the desktop", "float")
        self.movement.addItem("Walk along the taskbar", "walk")
        self.movement.addItem("Stay still", "still")
        self.movement.setCurrentIndex(max(0, self.movement.findData(s.movement)))

        self.speed = QSpinBox(suffix="%")
        self.speed.setRange(20, 400)
        self.speed.setSingleStep(10)
        self.speed.setValue(int(s.speed * 100))

        self.fps = QComboBox()
        for f in (15, 24, 30, 45, 60):
            self.fps.addItem(f"{f} FPS", f)
        idx = self.fps.findData(s.fps)
        if idx < 0:
            self.fps.addItem(f"{s.fps} FPS", s.fps)
            idx = self.fps.count() - 1
        self.fps.setCurrentIndex(idx)

        self.pitch = QSpinBox(suffix="°")
        self.pitch.setRange(-10, 45)
        self.pitch.setValue(int(round(s.pitch_deg)))

        self.outline = QCheckBox("Draw outline")
        self.outline.setChecked(s.outline)
        self.outline_width = QDoubleSpinBox(suffix=" px")
        self.outline_width.setRange(0.5, 6.0)
        self.outline_width.setSingleStep(0.25)
        self.outline_width.setValue(s.outline_width)
        self.outline.toggled.connect(self.outline_width.setEnabled)
        self.outline_width.setEnabled(s.outline)
        outline_row = QHBoxLayout()
        outline_row.addWidget(self.outline)
        outline_row.addWidget(self.outline_width)
        outline_row.addStretch()

        self.on_top = QCheckBox("Keep pets above other windows")
        self.on_top.setChecked(s.always_on_top)
        self.autostart = QCheckBox("Start when I log in")
        self.autostart.setChecked(s.launch_at_login)

        self.models_dir = QLineEdit(str(s.models_path))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: self._open(Path(self.models_dir.text())))
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.models_dir, 1)
        dir_row.addWidget(browse)
        dir_row.addWidget(open_btn)

        usage = sum(p.stat().st_size for p in s.models_path.glob("*.glb")) if s.models_path.exists() else 0
        self.usage = QLabel(f"Downloaded models use {human_size(usage) or '0 MB'}.")
        clear = QPushButton("Delete Downloaded Models…")
        clear.clicked.connect(self._clear)
        logs = QPushButton("Open Log Folder")
        logs.clicked.connect(lambda: self._open(log_dir()))
        usage_row = QHBoxLayout()
        usage_row.addWidget(self.usage, 1)
        usage_row.addWidget(clear)
        usage_row.addWidget(logs)

        form = QFormLayout()
        form.addRow("Pet size", size_row)
        form.addRow("Movement", self.movement)
        form.addRow("Walk speed", self.speed)
        form.addRow("Frame rate", self.fps)
        form.addRow("Camera angle", self.pitch)
        form.addRow("Outline", outline_row)
        form.addRow("", self.on_top)
        form.addRow("", self.autostart)
        form.addRow("Models folder", dir_row)
        form.addRow("", usage_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Models folder", self.models_dir.text())
        if path:
            self.models_dir.setText(path)

    @staticmethod
    def _open(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self, "Delete downloaded models",
            "Delete all downloaded model files? Pets currently on screen stay loaded; "
            "models are downloaded again when needed.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            freed = self.controller.clear_model_cache()
            self.usage.setText(f"Freed {human_size(freed) or '0 MB'}.")

    def _defaults(self) -> None:
        d = Settings()
        self.size_slider.setValue(int(d.size * 100))
        self.movement.setCurrentIndex(self.movement.findData(d.movement))
        self.speed.setValue(int(d.speed * 100))
        self.fps.setCurrentIndex(self.fps.findData(d.fps))
        self.pitch.setValue(int(d.pitch_deg))
        self.outline.setChecked(d.outline)
        self.outline_width.setValue(d.outline_width)
        self.on_top.setChecked(d.always_on_top)
        self.models_dir.setText(str(default_models_dir()))

    def _apply(self) -> None:
        models_dir = self.models_dir.text().strip()
        self.controller.apply_settings(
            size=self.size_slider.value() / 100.0,
            movement=self.movement.currentData(),
            speed=self.speed.value() / 100.0,
            fps=int(self.fps.currentData()),
            pitch_deg=float(self.pitch.value()),
            outline=self.outline.isChecked(),
            outline_width=float(self.outline_width.value()),
            always_on_top=self.on_top.isChecked(),
            launch_at_login=self.autostart.isChecked(),
            models_dir="" if Path(models_dir) == default_models_dir() else models_dir,
        )

    def _ok(self) -> None:
        self._apply()
        self.accept()


def show_about(parent: QWidget | None, icon: QIcon) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(f"About {APP_DISPLAY_NAME}")
    box.setIconPixmap(icon.pixmap(64, 64))
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<h3>{APP_DISPLAY_NAME} {__version__}</h3>"
        "<p>Animated 3D Blue Archive students that wander around your desktop.</p>"
        f"<p>Model list: <a href='{WIKI_URL}'>Blue Archive Wiki — Models</a><br>"
        "Model files: <a href='https://github.com/lihaohong6/BlueArchiveModels'>lihaohong6/BlueArchiveModels</a></p>"
        "<p><b>Controls:</b> drag a pet to pick it up, click to make it react, "
        "right-click for options.</p>"
        "<p style='color:gray'>Unofficial fan project. Blue Archive and its characters are "
        "trademarks/copyright of NEXON Games Co., Ltd. and Yostar. Models are downloaded "
        "from the sources above and are not distributed with this application.</p>"
    )
    box.exec()
