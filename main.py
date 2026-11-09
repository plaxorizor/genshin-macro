#!/usr/bin/env python3
"""Genshin Macro tray utility for KDE Plasma / Wayland.

Macros (only while the active window title contains "Genshin Impact"):
- Double-tap the same key 1-5 within a configurable interval: send E.
- Hold Pulsar XButton1: suppress the original button and repeat F.
- Hold Pulsar XButton2: suppress the original button and repeat left-click.

The physical mouse is grabbed while the app runs and forwarded through a
virtual device that reuses the physical mouse identity so KDE preserves its
per-device pointer settings. The app is manually launched; it installs no
service and creates no autostart entry.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from evdev import InputDevice, UInput, ecodes
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QMenu,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QSystemTrayIcon,
        QStyle,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    print(
        "Missing dependency. Install with:\n"
        "  sudo pacman -S python-evdev pyside6\n\n"
        f"Details: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)


# Hardware paths confirmed on the target CachyOS machine. Using by-id paths
# avoids relying on event numbers that can change between boots.
KEYBOARD_PATH = "/dev/input/by-id/usb-AKKO_M1_V5-if02-event-kbd"
MOUSE_PATH = "/dev/input/by-id/usb-pulsar_Pulsar_Mouse_IR_update-if02-event-ir"

GAME_TITLE = "Genshin Impact"
FOCUS_POLL_SECONDS = 0.250

NUMBER_KEYS = {
    ecodes.KEY_1,
    ecodes.KEY_2,
    ecodes.KEY_3,
    ecodes.KEY_4,
    ecodes.KEY_5,
}
XBUTTON1 = ecodes.BTN_SIDE
XBUTTON2 = ecodes.BTN_EXTRA

DEFAULT_CONFIG: dict[str, Any] = {
    "double_tap_enabled": True,
    "double_tap_ms": 200,
    "mouse4_enabled": True,
    "mouse4_min_ms": 90,
    "mouse4_max_ms": 110,
    "mouse5_enabled": True,
    "mouse5_min_ms": 80,
    "mouse5_max_ms": 110,
    "start_enabled": True,
}

BOOL_CONFIG_KEYS = (
    "double_tap_enabled",
    "mouse4_enabled",
    "mouse5_enabled",
    "start_enabled",
)
INT_CONFIG_RANGES = {
    "double_tap_ms": (10, 2000),
    "mouse4_min_ms": (10, 2000),
    "mouse4_max_ms": (10, 2000),
    "mouse5_min_ms": (10, 2000),
    "mouse5_max_ms": (10, 2000),
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if value is None:
        return default
    try:
        return bool(value)
    except Exception:
        return default


def normalize_config(values: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete, range-checked configuration dictionary."""
    source = values or {}
    config: dict[str, Any] = {}

    for key in BOOL_CONFIG_KEYS:
        config[key] = _as_bool(source.get(key, DEFAULT_CONFIG[key]), DEFAULT_CONFIG[key])

    for key, (minimum, maximum) in INT_CONFIG_RANGES.items():
        try:
            value = int(source.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError, OverflowError):
            value = DEFAULT_CONFIG[key]
        config[key] = max(minimum, min(maximum, value))

    # An interval must never be inverted. Preserve the lower bound and raise
    # the upper bound to match it if a malformed config is encountered.
    for prefix in ("mouse4", "mouse5"):
        low_key = f"{prefix}_min_ms"
        high_key = f"{prefix}_max_ms"
        if config[high_key] < config[low_key]:
            config[high_key] = config[low_key]

    return config


def load_config(settings) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, default in DEFAULT_CONFIG.items():
        values[key] = settings.value(key, default)
    return normalize_config(values)


def save_config(settings, config: dict[str, Any]) -> None:
    for key, value in normalize_config(config).items():
        settings.setValue(key, value)
    settings.sync()


class MacroEngine:
    """Input worker threads and macro state; Qt remains on the main thread."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.stop_event = threading.Event()
        self.game_active = threading.Event()

        self.config_lock = threading.Lock()
        self.config = normalize_config(config)
        self.enabled = self.config["start_enabled"]

        self.hold_f = threading.Event()
        self.hold_click = threading.Event()
        self.suppressed_buttons: set[int] = set()

        self.state_lock = threading.Lock()
        self.mouse_write_lock = threading.Lock()
        self.keyboard_write_lock = threading.Lock()

        self.last_key: int | None = None
        self.last_key_time = 0.0

        self.keyboard: InputDevice | None = None
        self.mouse: InputDevice | None = None
        self.keyboard_out: UInput | None = None
        self.mouse_out: UInput | None = None

        self.threads: list[threading.Thread] = []
        self.started = False

    def start(self) -> None:
        if not shutil.which("kdotool"):
            raise RuntimeError("kdotool was not found. Install it with: paru -S kdotool")

        if not Path("/dev/uinput").exists():
            raise RuntimeError(
                "/dev/uinput is missing. Load the module first:\n"
                "  sudo modprobe uinput"
            )

        self.keyboard = InputDevice(KEYBOARD_PATH)
        try:
            self.mouse = InputDevice(MOUSE_PATH)
        except Exception:
            self.keyboard.close()
            self.keyboard = None
            raise

        try:
            key_codes = set(
                self.keyboard.capabilities(absinfo=False).get(ecodes.EV_KEY, [])
            )
            mouse_codes = set(
                self.mouse.capabilities(absinfo=False).get(ecodes.EV_KEY, [])
            )
            if not NUMBER_KEYS.issubset(key_codes):
                raise RuntimeError("The selected AKKO device does not expose keys 1-5.")
            if not {XBUTTON1, XBUTTON2, ecodes.BTN_LEFT}.issubset(mouse_codes):
                raise RuntimeError(
                    "The selected Pulsar device does not expose XButton1, XButton2, and left-click."
                )

            self.keyboard_out = UInput(
                {ecodes.EV_KEY: [ecodes.KEY_E, ecodes.KEY_F]},
                name="Genshin Macro Keyboard",
            )

            # Reuse the physical mouse identity so KWin applies the same
            # per-device pointer settings. A separate phys string keeps the
            # virtual device distinguishable from the physical source.
            mouse_info = self.mouse.info
            self.mouse_out = UInput.from_device(
                self.mouse,
                name=self.mouse.name,
                vendor=mouse_info.vendor,
                product=mouse_info.product,
                version=mouse_info.version,
                bustype=mouse_info.bustype,
                phys="genshin-macro-virtual-mouse",
            )

            self.mouse.grab()

            self.started = True
            self._start_thread("keyboard-reader", self._keyboard_loop)
            self._start_thread("mouse-reader", self._mouse_loop)
            self._start_thread("window-monitor", self._focus_loop)
            self._start_thread(
                "repeat-f",
                lambda: self._repeat(
                    self.hold_f,
                    self._tap_key_f,
                    "mouse4_enabled",
                    "mouse4_min_ms",
                    "mouse4_max_ms",
                ),
            )
            self._start_thread(
                "repeat-click",
                lambda: self._repeat(
                    self.hold_click,
                    self._tap_left_click,
                    "mouse5_enabled",
                    "mouse5_min_ms",
                    "mouse5_max_ms",
                ),
            )
        except Exception:
            self.stop()
            raise

    def _start_thread(self, name: str, target) -> None:
        thread = threading.Thread(name=name, target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def is_enabled(self) -> bool:
        with self.config_lock:
            return self.enabled

    def set_enabled(self, enabled: bool) -> None:
        with self.config_lock:
            self.enabled = bool(enabled)
        if not enabled:
            self.hold_f.clear()
            self.hold_click.clear()
            self._reset_double_tap()

    def get_config(self) -> dict[str, Any]:
        with self.config_lock:
            return dict(self.config)

    def update_config(self, values: dict[str, Any]) -> None:
        new_config = normalize_config(values)
        with self.config_lock:
            old_config = self.config
            self.config = new_config
            disable_mouse4 = old_config["mouse4_enabled"] and not new_config["mouse4_enabled"]
            disable_mouse5 = old_config["mouse5_enabled"] and not new_config["mouse5_enabled"]
            disable_double_tap = (
                old_config["double_tap_enabled"] and not new_config["double_tap_enabled"]
            )

        if disable_mouse4:
            self.hold_f.clear()
        if disable_mouse5:
            self.hold_click.clear()
        if disable_double_tap:
            self._reset_double_tap()

    def _reset_double_tap(self) -> None:
        with self.state_lock:
            self.last_key = None
            self.last_key_time = 0.0

    def _config_value(self, key: str):
        with self.config_lock:
            return self.config[key]

    def _tap_key(self, key_code: int) -> None:
        assert self.keyboard_out is not None
        with self.keyboard_write_lock:
            self.keyboard_out.write(ecodes.EV_KEY, key_code, 1)
            self.keyboard_out.syn()
            self.keyboard_out.write(ecodes.EV_KEY, key_code, 0)
            self.keyboard_out.syn()

    def _tap_left_click(self) -> None:
        assert self.mouse_out is not None
        with self.mouse_write_lock:
            self.mouse_out.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
            self.mouse_out.syn()
            self.mouse_out.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
            self.mouse_out.syn()

    def _repeat(
        self,
        held: threading.Event,
        action,
        enabled_key: str,
        minimum_key: str,
        maximum_key: str,
    ) -> None:
        # One persistent worker per repeating macro; timing changes take effect
        # on the next repeat without spawning threads for individual presses.
        while not self.stop_event.is_set():
            if not held.wait(timeout=0.050):
                continue

            with self.config_lock:
                should_run = self.enabled and self.config[enabled_key]
                low = self.config[minimum_key] / 1000.0
                high = self.config[maximum_key] / 1000.0

            if should_run and self.game_active.is_set():
                action()

            self.stop_event.wait(random.uniform(low, high))

    def _keyboard_loop(self) -> None:
        assert self.keyboard is not None
        try:
            for event in self.keyboard.read_loop():
                if self.stop_event.is_set():
                    break
                if event.type != ecodes.EV_KEY or event.value != 1:
                    continue
                if event.code not in NUMBER_KEYS:
                    continue

                with self.config_lock:
                    should_handle = self.enabled and self.config["double_tap_enabled"]
                    double_tap_seconds = self.config["double_tap_ms"] / 1000.0

                if not should_handle or not self.game_active.is_set():
                    self._reset_double_tap()
                    continue

                now = time.monotonic()
                with self.state_lock:
                    send_e = (
                        self.last_key == event.code
                        and now - self.last_key_time <= double_tap_seconds
                    )
                    self.last_key = event.code
                    self.last_key_time = now

                if send_e:
                    self._tap_key(ecodes.KEY_E)
        except OSError as exc:
            if not self.stop_event.is_set():
                print(f"Keyboard input stopped: {exc}", file=sys.stderr)

    def _forward_mouse_batch(self, events) -> None:
        """Forward one evdev SYN_REPORT batch to the virtual mouse."""
        assert self.mouse_out is not None
        with self.mouse_write_lock:
            for event in events:
                if event.type != ecodes.EV_SYN:
                    self.mouse_out.write(event.type, event.code, event.value)
            self.mouse_out.syn()

    def _mouse_loop(self) -> None:
        assert self.mouse is not None
        batch = []
        try:
            for event in self.mouse.read_loop():
                if self.stop_event.is_set():
                    break

                if event.type == ecodes.EV_SYN:
                    if event.code == ecodes.SYN_REPORT:
                        self._forward_mouse_batch(batch)
                        batch.clear()
                    continue

                if event.type == ecodes.EV_KEY and event.code in (XBUTTON1, XBUTTON2):
                    # Always consume a release whose press was intercepted,
                    # even if focus or settings changed while it was held.
                    if event.value == 0 and event.code in self.suppressed_buttons:
                        self.suppressed_buttons.discard(event.code)
                        if event.code == XBUTTON1:
                            self.hold_f.clear()
                        else:
                            self.hold_click.clear()
                        continue

                    if event.value == 1:
                        with self.config_lock:
                            should_suppress = (
                                self.enabled
                                and self.game_active.is_set()
                                and self.config[
                                    "mouse4_enabled" if event.code == XBUTTON1 else "mouse5_enabled"
                                ]
                            )
                        if should_suppress:
                            self.suppressed_buttons.add(event.code)
                            if event.code == XBUTTON1:
                                self.hold_f.set()
                            else:
                                self.hold_click.set()
                            continue

                batch.append(event)
        except OSError as exc:
            if not self.stop_event.is_set():
                print(f"Mouse input stopped: {exc}", file=sys.stderr)

    def _tap_key_f(self) -> None:
        self._tap_key(ecodes.KEY_F)

    def _focus_loop(self) -> None:
        while not self.stop_event.is_set():
            active = False
            try:
                result = subprocess.run(
                    ["kdotool", "getactivewindow", "getwindowname"],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                    check=False,
                )
                active = (
                    result.returncode == 0
                    and GAME_TITLE.casefold() in result.stdout.casefold()
                )
            except (OSError, subprocess.TimeoutExpired):
                active = False

            if active:
                self.game_active.set()
            else:
                self.game_active.clear()
                self._reset_double_tap()
                self.hold_f.clear()
                self.hold_click.clear()

            self.stop_event.wait(FOCUS_POLL_SECONDS)

    def stop(self) -> None:
        self.stop_event.set()
        self.hold_f.clear()
        self.hold_click.clear()

        if self.mouse is not None:
            try:
                self.mouse.ungrab()
            except OSError:
                pass

        for thread in self.threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=0.25)

        for device in (self.keyboard, self.mouse):
            if device is not None:
                try:
                    device.close()
                except OSError:
                    pass

        for device in (self.keyboard_out, self.mouse_out):
            if device is not None:
                try:
                    device.close()
                except OSError:
                    pass

        self.keyboard = None
        self.mouse = None
        self.keyboard_out = None
        self.mouse_out = None
        self.started = False


class SettingsDialog(QDialog):
    """Genshin-only macro settings. Values are saved only on Save/OK."""

    def __init__(self, config: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Genshin Macro Settings")
        self.setMinimumWidth(390)
        self._original_config = normalize_config(config)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Configure the Genshin Impact macros. Changes are saved when you click Save.\n"
            "The macros still run only while Genshin Impact is the active window."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.double_group = QGroupBox("Double-tap number keys 1–5 → E")
        self.double_group.setCheckable(True)
        double_form = QFormLayout(self.double_group)
        self.double_ms = self._spin_box(10, 2000, self._original_config["double_tap_ms"])
        double_form.addRow("Maximum interval:", self.double_ms)
        root.addWidget(self.double_group)

        self.mouse4_group = QGroupBox("Hold Mouse 4 (XButton1) → repeat F")
        self.mouse4_group.setCheckable(True)
        mouse4_form = QFormLayout(self.mouse4_group)
        self.mouse4_min = self._spin_box(10, 2000, self._original_config["mouse4_min_ms"])
        self.mouse4_max = self._spin_box(10, 2000, self._original_config["mouse4_max_ms"])
        mouse4_form.addRow("Minimum interval:", self.mouse4_min)
        mouse4_form.addRow("Maximum interval:", self.mouse4_max)
        self._link_interval_bounds(self.mouse4_min, self.mouse4_max)
        root.addWidget(self.mouse4_group)

        self.mouse5_group = QGroupBox("Hold Mouse 5 (XButton2) → auto-click")
        self.mouse5_group.setCheckable(True)
        mouse5_form = QFormLayout(self.mouse5_group)
        self.mouse5_min = self._spin_box(10, 2000, self._original_config["mouse5_min_ms"])
        self.mouse5_max = self._spin_box(10, 2000, self._original_config["mouse5_max_ms"])
        mouse5_form.addRow("Minimum interval:", self.mouse5_min)
        mouse5_form.addRow("Maximum interval:", self.mouse5_max)
        self._link_interval_bounds(self.mouse5_min, self.mouse5_max)
        root.addWidget(self.mouse5_group)

        startup_group = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup_group)
        self.start_enabled = QCheckBox("Enable macros automatically when the app launches")
        startup_layout.addWidget(self.start_enabled)
        root.addWidget(startup_group)

        root.addStretch(1)

        footer = QHBoxLayout()
        self.reset_button = QPushButton("Restore defaults")
        self.reset_button.clicked.connect(self._restore_defaults)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)

        self._populate(self._original_config)

    @staticmethod
    def _spin_box(minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(5)
        spin.setSuffix(" ms")
        spin.setValue(value)
        return spin

    @staticmethod
    def _link_interval_bounds(minimum_spin: QSpinBox, maximum_spin: QSpinBox) -> None:
        minimum_spin.valueChanged.connect(
            lambda value: maximum_spin.setValue(max(value, maximum_spin.value()))
        )
        maximum_spin.valueChanged.connect(
            lambda value: minimum_spin.setValue(min(value, minimum_spin.value()))
        )

    def _populate(self, config: dict[str, Any]) -> None:
        cfg = normalize_config(config)
        self.double_group.setChecked(cfg["double_tap_enabled"])
        self.double_ms.setValue(cfg["double_tap_ms"])
        self.mouse4_group.setChecked(cfg["mouse4_enabled"])
        self.mouse4_min.setValue(cfg["mouse4_min_ms"])
        self.mouse4_max.setValue(cfg["mouse4_max_ms"])
        self.mouse5_group.setChecked(cfg["mouse5_enabled"])
        self.mouse5_min.setValue(cfg["mouse5_min_ms"])
        self.mouse5_max.setValue(cfg["mouse5_max_ms"])
        self.start_enabled.setChecked(cfg["start_enabled"])

    def _restore_defaults(self) -> None:
        self._populate(DEFAULT_CONFIG)

    def get_config(self) -> dict[str, Any]:
        return normalize_config(
            {
                "double_tap_enabled": self.double_group.isChecked(),
                "double_tap_ms": self.double_ms.value(),
                "mouse4_enabled": self.mouse4_group.isChecked(),
                "mouse4_min_ms": self.mouse4_min.value(),
                "mouse4_max_ms": self.mouse4_max.value(),
                "mouse5_enabled": self.mouse5_group.isChecked(),
                "mouse5_min_ms": self.mouse5_min.value(),
                "mouse5_max_ms": self.mouse5_max.value(),
                "start_enabled": self.start_enabled.isChecked(),
            }
        )


class TrayApp:
    def __init__(self, app: QApplication, engine: MacroEngine, settings) -> None:
        self.app = app
        self.engine = engine
        self.settings = settings

        self.menu = QMenu()
        self.enabled_action = QAction("Enable macros", self.menu)
        self.enabled_action.setCheckable(True)
        self.enabled_action.setChecked(self.engine.is_enabled())
        self.enabled_action.toggled.connect(self.engine.set_enabled)
        self.menu.addAction(self.enabled_action)

        self.status_action = QAction("Waiting for Genshin…", self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        self.menu.addSeparator()
        settings_action = QAction("Settings…", self.menu)
        settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(settings_action)

        self.menu.addSeparator()
        exit_action = QAction("Exit", self.menu)
        exit_action.triggered.connect(self.quit)
        self.menu.addAction(exit_action)

        fallback_icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        icon = QIcon.fromTheme(
            "applications-games",
            QIcon.fromTheme("input-gaming", fallback_icon),
        )
        self.tray = QSystemTrayIcon(icon, app)
        self.tray.setToolTip("Genshin Macro")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(500)
        self.refresh_status()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.refresh_status()
            self.tray.showMessage(
                "Genshin Macro",
                self.status_action.text(),
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

    def refresh_status(self) -> None:
        enabled = self.engine.is_enabled()
        active = self.engine.game_active.is_set()
        config = self.engine.get_config()
        any_macro_enabled = any(
            config[key]
            for key in ("double_tap_enabled", "mouse4_enabled", "mouse5_enabled")
        )
        if not enabled:
            status = "Macros disabled"
        elif active and not any_macro_enabled:
            status = "Genshin detected — no macros enabled"
        elif active:
            status = "Genshin detected — macros active"
        else:
            status = "Waiting for Genshin"

        self.status_action.setText(status)
        self.tray.setToolTip(f"Genshin Macro — {status}")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.engine.get_config())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        config = dialog.get_config()
        self.engine.update_config(config)
        save_config(self.settings, config)
        if self.settings.status() != QSettings.Status.NoError:
            QMessageBox.warning(
                None,
                "Settings could not be saved",
                "The new settings are active for this session, but KDE could not save them to disk.",
            )
        self.refresh_status()

    def quit(self) -> None:
        self.timer.stop()
        self.tray.hide()
        self.engine.stop()
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Genshin Macro")
    app.setOrganizationName("GenshinMacro")
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "System tray unavailable",
            "KDE's system tray is not currently available. Enable the System Tray widget and try again.",
        )
        return 1

    settings = QSettings("GenshinMacro", "GenshinMacro")
    config = load_config(settings)
    engine = MacroEngine(config)

    try:
        engine.start()
    except Exception as exc:
        engine.stop()
        QMessageBox.critical(None, "Genshin Macro could not start", str(exc))
        return 1

    tray = TrayApp(app, engine, settings)
    # Keep a Python reference alive for the lifetime of the Qt event loop.
    app._tray_app = tray  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
