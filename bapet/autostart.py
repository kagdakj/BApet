"""Launch-at-login integration (Windows registry Run key / XDG autostart)."""

from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path

from .config import APP_DISPLAY_NAME, APP_NAME, APP_SLUG, _xdg

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "bapet"]


def _desktop_file() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_SLUG}.desktop"


def set_enabled(enabled: bool) -> bool:
    """Enable/disable launch at login. Returns True on success."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    cmd = " ".join(f'"{part}"' for part in launch_command())
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        if sys.platform.startswith("linux"):
            path = _desktop_file()
            if enabled:
                path.parent.mkdir(parents=True, exist_ok=True)
                exec_line = " ".join(shlex.quote(p) for p in launch_command())
                path.write_text(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    f"Name={APP_DISPLAY_NAME}\n"
                    f"Exec={exec_line}\n"
                    "X-GNOME-Autostart-enabled=true\n"
                    "X-GNOME-Autostart-Delay=5\n"
                    "Terminal=false\n",
                    "utf-8",
                )
            else:
                path.unlink(missing_ok=True)
            return True
    except OSError:
        log.exception("could not change autostart setting")
        return False
    log.warning("autostart is not supported on %s", sys.platform)
    return False
