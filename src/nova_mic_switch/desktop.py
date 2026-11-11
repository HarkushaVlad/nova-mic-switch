"""Native desktop interface for explicitly pairing and switching capture mics."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QStandardPaths, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPushButton, QSlider, QSystemTrayIcon,
    QVBoxLayout, QWidget, QMenu, QScrollArea,
)
from PySide6.QtCore import QLockFile

from .audio_factory import create_audio_router, default_scope
from .feedback import AudioFeedback, encode_output_id
from .settings import AppSettings, SettingsError, load_settings, save_settings, validate_settings
from .startup import StartupManager, default_startup_command


class _Signals(QObject):
    done = Signal(object, object)
    event = Signal(object)
    source_done = Signal(int, object, object)
    startup_done = Signal(object, object)


class _Task(QRunnable):
    def __init__(self, fn, signals: _Signals, generation: int | None = None):
        super().__init__()
        self.fn, self.signals, self.generation = fn, signals, generation

    def run(self):
        try:
            result = self.fn()
            if self.generation is not None:
                self.signals.source_done.emit(self.generation, result, None)
            else:
                self.signals.done.emit(result, None)
        except Exception as exc:
            if self.generation is not None:
                self.signals.source_done.emit(self.generation, None, str(exc))
            else:
                self.signals.done.emit(None, str(exc))


def tray_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#8be0c0"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawRoundedRect(7, 8, 34, 32, 10, 10)
    painter.drawLine(16, 23, 22, 29)
    painter.drawLine(22, 29, 33, 17)
    painter.end()
    return QIcon(pixmap)


class InstanceCoordinator(QObject):
    """Per-config single-instance lock plus user-scoped activation socket."""

    activation = Signal(str)

    def __init__(self, config_dir: Path):
        super().__init__()
        canonical = str(Path(config_dir).expanduser().resolve())
        identity = f"{canonical}|{getattr(os, 'getuid', lambda: 'user')()}"
        server_basename = "nova-mic-switch-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]
        self.server_name = self._linux_server_path(server_basename) if sys.platform.startswith("linux") else server_basename
        self.lock = QLockFile(str(Path(config_dir) / ".nova-mic-switch.lock"))
        self.lock.setStaleLockTime(30_000)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.primary = False

    @staticmethod
    def _linux_server_path(basename: str) -> str:
        """Use a verified per-user runtime directory, independent of TMPDIR."""
        runtime_location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.RuntimeLocation
        )
        if not runtime_location:
            raise RuntimeError("Could not locate a per-user runtime directory for the activation socket.")
        runtime_dir = Path(runtime_location)
        if not runtime_dir.is_absolute():
            raise RuntimeError("The per-user runtime directory for the activation socket must be absolute.")
        try:
            runtime_stat = runtime_dir.lstat()
        except OSError as exc:
            raise RuntimeError(f"Could not inspect the per-user runtime directory {runtime_dir}: {exc}") from exc
        if not stat.S_ISDIR(runtime_stat.st_mode):
            raise RuntimeError(f"The per-user runtime location is not a real directory: {runtime_dir}")
        if hasattr(os, "getuid") and runtime_stat.st_uid != os.getuid():
            raise RuntimeError(f"The per-user runtime directory is not owned by this user: {runtime_dir}")
        if runtime_stat.st_mode & 0o077:
            raise RuntimeError(f"The per-user runtime directory must not be accessible by other users: {runtime_dir}")
        if not os.access(runtime_dir, os.W_OK | os.X_OK):
            raise RuntimeError(f"The per-user runtime directory is not writable: {runtime_dir}")
        server_path = str(runtime_dir / basename)
        path_length = len(os.fsencode(server_path))
        # Linux sockaddr_un.sun_path holds 108 bytes including its NUL terminator;
        # Qt rejects paths of 107 bytes, so keep the actual path at most 106 bytes.
        if path_length > 106:
            raise RuntimeError(
                f"The per-user runtime path is too long for a local activation socket ({path_length} bytes; maximum 106): {runtime_dir}"
            )
        return server_path

    def acquire_or_notify(self, command: str) -> bool:
        Path(self.lock.fileName()).parent.mkdir(parents=True, exist_ok=True)
        if not self.lock.tryLock(0):
            for _ in range(5):
                socket = QLocalSocket()
                socket.connectToServer(self.server_name)
                if socket.waitForConnected(100):
                    socket.write(command.encode("ascii"))
                    socket.waitForBytesWritten(250)
                    socket.disconnectFromServer()
                    break
            return False
        # The exclusive lock proves no live owner can be using this endpoint.
        QLocalServer.removeServer(self.server_name)
        if not self.server.listen(self.server_name):
            self.lock.unlock()
            raise RuntimeError(f"Could not create single-instance endpoint: {self.server.errorString()}")
        self.primary = True
        return True

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self.activation.emit(bytes(s.readAll()).decode("ascii", "ignore")))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self.activation.emit(bytes(socket.readAll()).decode("ascii", "ignore"))

    def close(self):
        if self.primary:
            self.server.close()
            self.lock.unlock()
            self.primary = False


STYLE = """
QWidget { background: #171b20; color: #edf1f4; font-size: 14px; }
QMainWindow { background: #171b20; }
QLabel#eyebrow { color: #8be0c0; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#title { font-size: 25px; font-weight: 650; }
QLabel#muted { color: #a5afb8; }
QFrame#card { background: #20262d; border: 1px solid #343c45; border-radius: 12px; }
QComboBox { background: #14181d; border: 1px solid #46515c; border-radius: 7px; padding: 9px; min-height: 20px; }
QComboBox:focus, QPushButton:focus { border: 1px solid #8be0c0; }
QComboBox QAbstractItemView { background: #20262d; selection-background-color: #315948; }
QPushButton { background: #303942; border: 1px solid #46515c; border-radius: 8px; padding: 9px 14px; }
QPushButton:hover { background: #3b4650; }
QPushButton#primary { background: #8be0c0; color: #10251e; border: 0; font-weight: 700; padding: 11px 20px; }
QPushButton#primary:hover { background: #a6efd4; }
QPushButton:disabled { color: #7b858e; background: #272d33; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QSlider::groove:horizontal { height: 4px; background: #414b55; }
QSlider::handle:horizontal { background: #8be0c0; width: 14px; margin: -5px 0; border-radius: 7px; }
"""


class DesktopWindow(QMainWindow):
    def __init__(self, config_dir: Path, *, safe_mode: bool = False, service_factory=None,
                 router_factory=create_audio_router, feedback_factory=AudioFeedback,
                 tray_available: bool | None = None, startup_manager=None, startup_factory=None):
        super().__init__()
        self.config_dir = Path(config_dir)
        self.safe_mode = safe_mode
        self.router_factory = router_factory
        self.service_factory = service_factory
        self.feedback_factory = feedback_factory
        self.pool = QThreadPool.globalInstance()
        self.signals = _Signals(self)
        self.signals.source_done.connect(self._sources_loaded)
        self.signals.startup_done.connect(self._startup_result)
        self.signals.done.connect(self._startup_result)
        self.signals.event.connect(self._service_event)
        self.service = None
        self.sources = []
        self._source_generation = 0
        self._config_locked = False
        self._closing = False
        self._feedback_warning_text = ""
        self._autostart_pending = False
        self._pair_confirmed = False
        self._consent_pending = False
        self._tray_action = None
        self.startup_manager = startup_manager
        self.startup_factory = startup_factory or StartupManager
        self._startup_busy = False
        self._startup_actual = False
        self._startup_mutation = False
        self._startup_state_known = False
        self._startup_error_text = ""
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        self.config_existed = (self.config_dir / "settings.json").is_file()
        try:
            self.settings = load_settings(self.config_dir)
        except SettingsError as exc:
            self.settings = AppSettings()
            self.load_error = str(exc)
        else:
            self.load_error = ""
        self._pair_confirmed = bool(self.config_existed and self.settings.auto_start)
        self.setWindowTitle("Nova Mic Switch")
        self.setMinimumSize(500, 600)
        self.resize(580, 820)
        self.setWindowIcon(tray_icon())
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._setup_tray()
        if self.load_error:
            self.status.setText("Settings need attention")
            self.detail.setText(self.load_error)
            self.reset_button.show()
        elif self.safe_mode:
            self.status.setText("Safe mode · no devices opened")
            self.start_button.setEnabled(False)
        else:
            self.refresh_sources()
            self._autostart_pending = bool(self.config_existed and self.settings.auto_start and not safe_mode)
            self._initialize_startup()
        try:
            self.feedback = None if safe_mode else self.feedback_factory(
                self.config_dir, output_id=self.settings.feedback_output_id,
                enabled=self.settings.feedback_enabled, volume=self.settings.feedback_volume,
                warning=self._feedback_warning,
            )
        except Exception as exc:
            self.feedback = None
            self._feedback_warning(f"Sound cues are unavailable: {exc}. Microphone switching can still work.")
            self.test_sound.setEnabled(False)
        if self.feedback is not None and hasattr(self.feedback, "devices"):
            self.feedback.devices.audioOutputsChanged.connect(self.refresh_outputs)
        if not safe_mode:
            self.refresh_outputs()
        else:
            self.test_sound.setEnabled(False)
            self.feedback_check.setEnabled(False)
            self.resume_check.setEnabled(False)
            self.login_check.setEnabled(False)
        if self.load_error:
            self._set_config_enabled(False)
        if self.safe_mode:
            self._set_config_enabled(False)
            self.reset_button.setEnabled(False)

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 25, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(QLabel("MICROPHONE ROUTING", objectName="eyebrow"))
        layout.addWidget(QLabel("Nova Mic Switch", objectName="title"))
        layout.addWidget(QLabel("Choose the two microphones this button should switch between.", objectName="muted"))

        card = QFrame(objectName="card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 17, 18, 18)
        form.setVerticalSpacing(13)
        self.desktop_combo = QComboBox()
        self.headset_combo = QComboBox()
        self.desktop_combo.currentIndexChanged.connect(self._pair_changed)
        self.headset_combo.currentIndexChanged.connect(self._pair_changed)
        form.addRow("Computer microphone", self.desktop_combo)
        form.addRow("Headset microphone", self.headset_combo)
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItem("Previous track · POWER × 3", "previous-track")
        self.trigger_combo.addItem("Next track · POWER × 2", "next-track")
        self.trigger_combo.addItem("Play / pause · POWER × 1", "play-pause")
        self.trigger_combo.setCurrentIndex(max(0, self.trigger_combo.findData(self.settings.trigger)))
        self.trigger_combo.currentIndexChanged.connect(self._save_settings)
        form.addRow("Headset button", self.trigger_combo)
        layout.addWidget(card)

        self.scope_combo = QComboBox()
        if sys.platform.startswith("linux"):
            self.scope_combo.addItem("Discord only", "discord")
            self.scope_combo.addItem("All apps · system default", "system")
        else:
            self.scope_combo.addItem("All apps · system default", "system")
        self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(self.settings.scope)))
        self.scope_combo.currentIndexChanged.connect(self._save_settings)
        self.scope_combo.currentIndexChanged.connect(self.refresh_sources)
        scope_card = QFrame(objectName="card")
        scope_form = QFormLayout(scope_card)
        scope_form.setContentsMargins(18, 14, 18, 14)
        scope_form.addRow("Applies to", self.scope_combo)
        layout.addWidget(scope_card)

        self.status = QLabel("Checking available microphones…")
        self.status.setStyleSheet("font-weight: 650; color: #8be0c0")
        self.detail = QLabel("POWER also controls media. Hold for about one second to turn the headset off; that is not a switch.", objectName="muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(self.detail)

        actions = QHBoxLayout()
        self.start_button = QPushButton("Start listening")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle_service)
        self.refresh_button = QPushButton("Refresh microphones")
        self.refresh_button.clicked.connect(self.refresh_sources)
        actions.addWidget(self.start_button)
        actions.addWidget(self.refresh_button)
        layout.addLayout(actions)

        self.feedback_check = QCheckBox("Play a quiet sound after a confirmed switch")
        self.feedback_check.setChecked(self.settings.feedback_enabled)
        self.feedback_check.toggled.connect(self._feedback_changed)
        layout.addWidget(self.feedback_check)
        self.resume_check = QCheckBox("Resume listening when Nova Mic Switch opens")
        self.resume_check.setChecked(self.settings.auto_start if self.config_existed else True)
        self.resume_check.setToolTip("Available after you confirm and start this microphone pair once.")
        self.resume_check.toggled.connect(self._save_settings)
        layout.addWidget(self.resume_check)
        self.login_check = QCheckBox("Start Nova Mic Switch when I sign in")
        self.login_check.setChecked(self.settings.start_at_login)
        self.login_check.toggled.connect(self._login_toggled)
        layout.addWidget(self.login_check)
        self.minimized_check = QCheckBox("Open minimized in the tray")
        self.minimized_check.setChecked(self.settings.start_minimized)
        self.minimized_check.setToolTip("When the tray is unavailable, the app opens as a normal window instead.")
        self.minimized_check.toggled.connect(self._minimized_toggled)
        layout.addWidget(self.minimized_check)
        self.test_sound = QPushButton("Test sound")
        self.test_sound.setToolTip("Plays a test tone only; it does not switch microphones.")
        self.test_sound.clicked.connect(lambda: self.feedback and self.feedback.play("test"))
        layout.addWidget(self.test_sound, alignment=Qt.AlignmentFlag.AlignLeft)

        self.advanced = QPushButton("Advanced sound settings  ▾")
        self.advanced.setCheckable(True)
        self.advanced.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced)
        self.advanced_panel = QFrame(objectName="card")
        advanced_form = QFormLayout(self.advanced_panel)
        advanced_form.setContentsMargins(18, 14, 18, 14)
        self.output_combo = QComboBox()
        self.output_combo.currentIndexChanged.connect(self._output_changed)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(round(self.settings.feedback_volume * 100))
        self.volume.valueChanged.connect(self._volume_changed)
        advanced_form.addRow("Sound output", self.output_combo)
        advanced_form.addRow("Cue volume", self.volume)
        self.advanced_panel.hide()
        layout.addWidget(self.advanced_panel)
        self.reset_button = QPushButton("Reset damaged settings…")
        self.reset_button.clicked.connect(self._recover_settings)
        self.reset_button.hide()
        layout.addWidget(self.reset_button)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

    def _setup_tray(self):
        self.tray = None
        if not self.tray_available:
            return
        self.tray = QSystemTrayIcon(tray_icon(), self)
        self.tray.setToolTip("Nova Mic Switch · stopped")
        menu = QMenu(self)
        self._tray_menu = menu
        open_action = QAction("Open Nova Mic Switch", self)
        open_action.triggered.connect(self.showNormal)
        self._tray_action = QAction("Start listening", self)
        self._tray_action.triggered.connect(self.toggle_service)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(open_action)
        menu.addAction(self._tray_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def refresh_sources(self):
        if self.safe_mode or self._config_locked:
            return
        self._source_generation += 1
        generation = self._source_generation
        self.status.setText("Checking available microphones…")
        scope = self.scope_combo.currentData()
        desktop, headset = self.settings.desktop_source, self.settings.headset_source
        def inspect_sources():
            router = self.router_factory(scope)
            sources = router.list_sources()
            try:
                current = router.current_source()
                current_error = ""
            except Exception as exc:
                current, current_error = "", str(exc)
            return sources, current, desktop, headset, current_error
        self.pool.start(_Task(inspect_sources, self.signals, generation))

    def _initialize_startup(self):
        if self.safe_mode or self.load_error:
            self.login_check.setEnabled(False)
            self.minimized_check.setEnabled(False)
            return
        try:
            manager = self.startup_manager or self.startup_factory(default_startup_command(self.config_dir))
            self.startup_manager = manager
            if not manager.available:
                raise RuntimeError("Login startup is not supported on this platform.")
        except Exception as exc:
            self.login_check.setEnabled(False)
            self.minimized_check.setEnabled(False)
            self._startup_error_text = f"Login startup is unavailable: {exc}"
            self.detail.setText(self._startup_error_text)
            return
        self._startup_busy = True
        self.login_check.setEnabled(False)
        self.minimized_check.setEnabled(False)
        self.pool.start(_Task(manager.is_enabled, self.signals))

    def _startup_result(self, actual, error):
        if not self._startup_busy:
            return
        self._startup_busy = False
        mutation = self._startup_mutation
        self._startup_mutation = False
        self.login_check.setEnabled(not self.safe_mode and not self.load_error)
        self.minimized_check.setEnabled(not self.safe_mode and not self.load_error)
        if error:
            self._startup_state_known = False
            self._set_login_check_state(partial=True)
            self.login_check.setEnabled(False)
            self.minimized_check.setEnabled(False)
            self._startup_error_text = f"Could not {'change' if mutation else 'check'} login startup: {error}"
            self.detail.setText(self._startup_error_text)
            return
        requested_minimized = self.minimized_check.isChecked()
        if isinstance(actual, tuple):
            actual_enabled, operation_error, mutation_failed, unknown = actual
            if unknown:
                self._startup_state_known = False
                self._set_login_check_state(partial=True)
                self.login_check.setEnabled(False)
                self.minimized_check.setEnabled(False)
                self._startup_error_text = operation_error
                self.detail.setText(operation_error)
                return
            if mutation_failed:
                self._startup_state_known = True
                self._startup_actual = bool(actual_enabled)
                self._set_login_check_state(checked=self._startup_actual)
                self.minimized_check.blockSignals(True)
                self.minimized_check.setChecked(self.settings.start_minimized)
                self.minimized_check.blockSignals(False)
                try:
                    self.settings = replace(self.settings, start_at_login=self._startup_actual)
                    save_settings(self.config_dir, self.settings)
                except (SettingsError, OSError) as exc:
                    operation_error += f" The preference could not be saved: {exc}"
                self._startup_error_text = operation_error
                self.detail.setText(operation_error)
                return
            actual = actual_enabled
        self._startup_error_text = ""
        self._startup_state_known = True
        self._startup_actual = bool(actual)
        self._set_login_check_state(checked=bool(actual))
        updated = replace(self.settings, start_at_login=bool(actual),
                          start_minimized=requested_minimized if mutation else self.settings.start_minimized)
        try:
            save_settings(self.config_dir, updated)
            self.settings = updated
        except (SettingsError, OSError) as exc:
            prefix = "Login startup changed" if mutation else "Login startup was checked"
            self._startup_error_text = f"{prefix}, but its preference could not be saved: {exc}"
            self.detail.setText(self._startup_error_text)

    def _set_login_check_state(self, *, checked: bool | None = None, partial: bool = False):
        previous = self.login_check.blockSignals(True)
        try:
            self.login_check.setTristate(partial)
            if partial:
                self.login_check.setCheckState(Qt.CheckState.PartiallyChecked)
            elif checked is not None:
                self.login_check.setChecked(checked)
        finally:
            self.login_check.blockSignals(previous)

    def _login_toggled(self, enabled):
        if self.safe_mode or self.load_error or self._startup_busy or not self._startup_state_known or self.startup_manager is None:
            return
        self._change_startup(bool(enabled), self.minimized_check.isChecked())

    def _minimized_toggled(self, minimized):
        if self.safe_mode or self.load_error or not self._startup_state_known:
            return
        if self.login_check.isChecked() and self.startup_manager is not None and not self._startup_busy:
            self._change_startup(True, bool(minimized))
        else:
            self._save_settings()

    def _change_startup(self, enabled, minimized):
        if not self._startup_state_known:
            return
        self._startup_busy = True
        self._startup_mutation = True
        self.login_check.setEnabled(False)
        self.minimized_check.setEnabled(False)
        manager = self.startup_manager
        def change_and_verify():
            try:
                manager.set_enabled(enabled, start_minimized=minimized)
                return manager.is_enabled()
            except Exception as exc:
                try:
                    current = manager.is_enabled()
                except Exception as read_exc:
                    return (None, f"Could not change login startup; its current state is unknown: {exc}; {read_exc}", True, True)
                return (current, f"Could not change login startup: {exc}", True, False)
        self.pool.start(_Task(change_and_verify, self.signals))


    def _sources_loaded(self, generation, result, error):
        if generation != self._source_generation:
            return
        if error:
            self.status.setText("Could not list microphones")
            self.detail.setText(f"{error} Reconnect the audio device, then choose Refresh microphones.")
            if self._autostart_pending:
                self._autostart_pending = False
                try:
                    validate_settings(self.settings, require_sources=True)
                    self._start_service(confirmed=True, automatic=True)
                    self.status.setText("Reconnecting to saved microphones…")
                    self.detail.setText("Listening will resume when the saved microphones and headset button are available.")
                except Exception as exc:
                    self.status.setText("Saved microphone pair needs attention")
                    self.detail.setText(f"Could not resume listening: {exc}")
            return
        sources, current, configured_desktop, configured_headset, current_error = result
        self.sources = list(sources)
        old_desktop, old_headset = self.desktop_combo.currentData(), self.headset_combo.currentData()
        self.desktop_combo.blockSignals(True)
        self.headset_combo.blockSignals(True)
        for combo in (self.desktop_combo, self.headset_combo):
            combo.clear()
            combo.addItem("Select a microphone…", "")
            for source in self.sources:
                label = source.name + (" · system default" if source.is_default else "")
                combo.addItem(label, source.id)
                combo.setItemData(combo.count() - 1, f"{source.name}\nCapture ID: {source.id}", Qt.ItemDataRole.ToolTipRole)
        for combo, wanted in ((self.desktop_combo, self.settings.desktop_source or old_desktop), (self.headset_combo, self.settings.headset_source or old_headset)):
            index = combo.findData(wanted)
            if index >= 0:
                combo.setCurrentIndex(index)
            elif wanted:
                combo.addItem(f"Saved microphone · unavailable ({wanted})", wanted)
                combo.setItemData(combo.count() - 1, f"Saved capture ID: {wanted}\nReconnect this microphone or select another.", Qt.ItemDataRole.ToolTipRole)
                combo.setCurrentIndex(combo.count() - 1)
            elif combo is self.desktop_combo:
                default_candidates = [i for i, source in enumerate(self.sources, 1) if source.is_default and not self._is_nova_source(source)]
                if len(default_candidates) == 1:
                    combo.setCurrentIndex(default_candidates[0])
            elif combo is self.headset_combo:
                nova_candidates = [i for i, source in enumerate(self.sources, 1) if self._is_nova_source(source)]
                if len(nova_candidates) == 1:
                    combo.setCurrentIndex(nova_candidates[0])
        self.desktop_combo.blockSignals(False)
        self.headset_combo.blockSignals(False)
        self.status.setText("Microphones ready" if self.sources else "No microphones found")
        if current and current == configured_desktop:
            self.status.setText("Computer microphone active")
        elif current and current == configured_headset:
            self.status.setText("Headset microphone active")
        elif current and configured_desktop and configured_headset:
            active_name = next((source.name for source in self.sources if source.id == current), "An unlisted microphone")
            self.status.setText("Microphone outside selected pair")
            self.detail.setText(f"{active_name} is active, but is outside your pair. Set either selected microphone as the input in Discord or system settings, then switch.")
        elif current_error and self.scope_combo.currentData() == "discord":
            self.detail.setText("Microphones are listed. Current Discord capture is checked when you start or join a voice call.")
        else:
            self.detail.setText("Confirm your microphones, then start listening.")
        self._pair_changed()

        if self._autostart_pending:
            self._autostart_pending = False
            try:
                validate_settings(self.settings, require_sources=True)
                self._start_service(confirmed=True, automatic=True)
                self.status.setText("Reconnecting to saved microphones…")
                self.detail.setText("Listening will resume when the saved microphones and headset button are available.")
            except Exception as exc:
                self.status.setText("Saved microphone pair needs attention")
                self.detail.setText(f"Could not resume listening: {exc}")
        if self._feedback_warning_text:
            self.detail.setText(self._feedback_warning_text)
        if self._startup_error_text:
            self.detail.setText(self._startup_error_text)

    @staticmethod
    def _is_nova_source(source):
        return "nova 5" in (source.name + " " + source.id).lower()

    def _pair_is_available(self):
        available = {source.id for source in self.sources}
        return bool(self.desktop_combo.currentData() and self.headset_combo.currentData()
                    and self.desktop_combo.currentData() != self.headset_combo.currentData()
                    and self.desktop_combo.currentData() in available and self.headset_combo.currentData() in available)

    def _pair_changed(self, *_):
        desktop, headset = self.desktop_combo.currentData(), self.headset_combo.currentData()
        valid = bool(desktop and headset and desktop != headset)
        self.start_button.setEnabled(valid and not self.safe_mode and not self.load_error and not self._config_locked)
        if desktop and headset and desktop == headset:
            self.detail.setText("Choose two different microphones. The same source cannot be both sides of the switch.")
        elif desktop and headset and (desktop not in {s.id for s in self.sources} or headset not in {s.id for s in self.sources}):
            self.start_button.setEnabled(False)
            self.detail.setText("A saved microphone is unavailable. Reconnect it or choose another microphone.")
        self._save_settings()

    def _save_settings(self, *_):
        if not hasattr(self, "desktop_combo") or self.safe_mode or self.load_error:
            return
        try:
            settings = AppSettings(
                desktop_source=self.desktop_combo.currentData() or "",
                headset_source=self.headset_combo.currentData() or "",
                trigger=self.trigger_combo.currentData() if hasattr(self, "trigger_combo") else self.settings.trigger,
                scope=self.scope_combo.currentData() or default_scope(),
                feedback_enabled=self.feedback_check.isChecked() if hasattr(self, "feedback_check") else self.settings.feedback_enabled,
                feedback_output_id=self.output_combo.currentData() or "" if hasattr(self, "output_combo") else self.settings.feedback_output_id,
                feedback_volume=self.volume.value() / 100 if hasattr(self, "volume") else self.settings.feedback_volume,
                start_at_login=self._startup_actual if hasattr(self, "login_check") else self.settings.start_at_login,
                start_minimized=self.minimized_check.isChecked() if hasattr(self, "minimized_check") else self.settings.start_minimized,
                auto_start=(self.resume_check.isChecked() if self._pair_confirmed or self._consent_pending else False)
                if hasattr(self, "resume_check") else self.settings.auto_start,
            )
            pair_changed = any(getattr(settings, name) != getattr(self.settings, name)
                               for name in ("desktop_source", "headset_source", "trigger", "scope"))
            if pair_changed and self._pair_confirmed and not self._consent_pending:
                self._pair_confirmed = False
                self.resume_check.blockSignals(True)
                self.resume_check.setChecked(False)
                self.resume_check.blockSignals(False)
                settings = replace(settings, auto_start=False)
            validate_settings(settings)
            save_settings(self.config_dir, settings)
            self.settings = settings
            self.persist_error = ""
        except (SettingsError, OSError) as exc:
            self.persist_error = str(exc)
            self.detail.setText(f"Settings were not saved: {exc}")
            self.start_button.setEnabled(False)

    def _feedback_changed(self, enabled):
        if self.feedback:
            self.feedback.set_enabled(enabled)
        self._save_settings()

    def _toggle_advanced(self, visible):
        self.advanced_panel.setVisible(visible)
        self.advanced.setText("Advanced sound settings  ▴" if visible else "Advanced sound settings  ▾")

    def refresh_outputs(self):
        wanted = self.output_combo.currentData() or self.settings.feedback_output_id
        self.output_combo.blockSignals(True)
        self.output_combo.clear()
        self.output_combo.addItem("Choose an output…", "")
        try:
            outputs = self.feedback_factory.outputs()
        except Exception:
            outputs = []
        for output in outputs:
            encoded_id = encode_output_id(bytes(output.id()))
            self.output_combo.addItem(output.description(), encoded_id)
            self.output_combo.setItemData(self.output_combo.count() - 1, f"Output ID (base64): {encoded_id}", Qt.ItemDataRole.ToolTipRole)
        if wanted:
            index = self.output_combo.findData(wanted)
            if index < 0:
                self.output_combo.addItem("Previously selected output · unavailable", wanted)
                index = self.output_combo.count() - 1
                self._feedback_warning("The selected sound output is unavailable. Choose another output; cues will not use your default speakers.")
            else:
                self._feedback_warning_text = ""
        else:
            matches = [i for i in range(1, self.output_combo.count()) if "nova" in self.output_combo.itemText(i).lower()]
            index = matches[0] if len(matches) == 1 else 0
        self.output_combo.setCurrentIndex(max(0, index))
        self.output_combo.blockSignals(False)
        if self.feedback:
            self.feedback.set_output(self.output_combo.currentData() or "")

    def _output_changed(self, *_):
        if self.feedback:
            self.feedback.set_output(self.output_combo.currentData() or "")
        self._save_settings()

    def _volume_changed(self, value):
        if self.feedback:
            self.feedback.set_volume(value / 100)
        self._save_settings()

    def _feedback_warning(self, message):
        self._feedback_warning_text = message
        self.detail.setText(message)

    def _set_config_enabled(self, enabled):
        for widget in (self.desktop_combo, self.headset_combo, self.trigger_combo, self.scope_combo,
                       self.feedback_check, self.resume_check, self.output_combo, self.volume,
                       self.refresh_button, self.test_sound):
            widget.setEnabled(enabled and not self.safe_mode)
        self.start_button.setEnabled(enabled and not self.safe_mode and not self.load_error and not getattr(self, "persist_error", "") and self._pair_is_available())

    def _recover_settings(self):
        answer = QMessageBox.question(self, "Reset settings", "The settings file is damaged. Replace it with fresh defaults? The current file will be replaced only after you confirm.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            fresh = AppSettings(auto_start=False)
            save_settings(self.config_dir, fresh)
        except Exception as exc:
            self.detail.setText(f"Could not reset settings: {exc}")
            return
        self.settings = fresh
        self.load_error = ""
        self.reset_button.hide()
        self._set_config_enabled(True)
        self.status.setText("Settings reset · choose your microphones")
        self.refresh_sources()

    def toggle_service(self):
        if self.service is not None:
            self._request_service_stop()
            return
        try:
            validate_settings(self.settings, require_sources=True)
            if not self._pair_is_available():
                raise SettingsError("One or both selected microphones are unavailable. Retry devices or choose another pair.")
            answer = QMessageBox.question(
                self, "Confirm microphone pair",
                self._confirmation_text(),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._consent_pending = True
            self._save_settings()
            self._consent_pending = False
            if self.persist_error:
                raise SettingsError(self.persist_error)
            self._pair_confirmed = True
            self._start_service(confirmed=True)
        except Exception as exc:
            self.detail.setText(str(exc))
            QMessageBox.warning(self, "Could not start", str(exc))

    def _confirmation_text(self):
        desktop_name = next((s.name for s in self.sources if s.id == self.settings.desktop_source), self.settings.desktop_source)
        headset_name = next((s.name for s in self.sources if s.id == self.settings.headset_source), self.settings.headset_source)
        scope_note = ("Discord only: Discord’s active voice-capture input will change. This does not change other apps."
                      if self.settings.scope == "discord" else
                      "System-wide: the default input changes for Discord and other apps.")
        return (f"Switch between {desktop_name} and {headset_name}?\n\n{scope_note}\n\n"
                "POWER may also control media playback. A short press plays/pauses; double/triple presses may change tracks. A hold can power off the headset.")

    def _start_service(self, *, confirmed: bool, automatic: bool = False):
        if self.service is not None:
            return
        from .service import SwitchService
        factory = self.service_factory or SwitchService
        self._set_config_enabled(False)
        try:
            self.service = factory(self.settings, self.signals.event.emit, apply=True)
            self.service.start()
        except Exception:
            self.service = None
            self._set_config_enabled(True)
            raise
        self.start_button.setText("Stop listening")
        self.start_button.setEnabled(True)
        self.status.setText("Connecting to headset button…")
        if not automatic:
            self.detail.setText("Listening runs in the background. The headset POWER button may also control media playback.")
        self._update_tray_action()

    def _request_service_stop(self):
        if self.service is None:
            return
        self.start_button.setText("Stopping…")
        self.start_button.setEnabled(False)
        self.status.setText("Stopping safely…")
        self._closing = self._closing or False
        try:
            stopped = self.service.stop(timeout=0)
        except TypeError:
            stopped = self.service.stop(0)
        if stopped:
            self._service_stopped()
        else:
            QTimer.singleShot(80, self._poll_service_stop)

    def _poll_service_stop(self):
        if self.service is None:
            return
        if not self.service.running:
            self._service_stopped()
        else:
            QTimer.singleShot(80, self._poll_service_stop)

    def _service_stopped(self):
        self.service = None
        self.start_button.setText("Start listening")
        self.status.setText("Stopped")
        self._set_config_enabled(True)
        self._update_tray_action()
        if self._closing:
            self._finish_close()

    def _update_tray_action(self):
        if self._tray_action:
            running = self.service is not None
            self._tray_action.setText("Stop listening" if running else "Start listening")
            if self.tray:
                self.tray.setToolTip("Nova Mic Switch · listening for headset POWER" if running else "Nova Mic Switch · stopped")
        self.start_button.setToolTip("Stop microphone button listening" if self.service is not None else "Start listening for the headset POWER button")

    def _service_event(self, event):
        kind = event.get("type")
        if kind == "state":
            state = event.get("state")
            self.status.setText({"connecting": "Connecting to headset button…", "listening": "Listening for the headset button", "stopped": "Stopped"}.get(state, str(state)))
            if self.tray:
                self.tray.setToolTip({"connecting": "Nova Mic Switch · connecting", "listening": "Nova Mic Switch · listening for headset POWER", "stopped": "Nova Mic Switch · stopped"}.get(state, "Nova Mic Switch"))
            if state == "stopped" and self.service is not None:
                QTimer.singleShot(0, self._poll_service_stop)
        elif kind == "switch":
            target = event.get("target_source")
            if self.feedback and event.get("applied") is True and event.get("dry_run") is False and target in (self.settings.desktop_source, self.settings.headset_source):
                self.feedback.route_succeeded(event, desktop_source=self.settings.desktop_source, headset_source=self.settings.headset_source)
            if event.get("applied") is not True or event.get("dry_run") is True:
                intended = "computer microphone" if target == self.settings.desktop_source else "headset microphone" if target == self.settings.headset_source else "the configured pair"
                self.status.setText(f"Preview only · would switch to {intended}")
            elif target == self.settings.desktop_source:
                self.status.setText("Computer microphone active")
            elif target == self.settings.headset_source:
                self.status.setText("Headset microphone active")
            else:
                self.status.setText("Button detected · no configured target")
        elif kind == "error":
            message = event.get("message", "A device or audio error occurred.")
            if self.settings.scope == "discord" and "no Discord recording stream" in message:
                message = "No active Discord voice capture was found. Join or start a voice call, then try the headset button again."
            elif "outside the configured pair" in message:
                message = "The current microphone is outside your selected pair. Set the computer or headset microphone as active, then switch."
            elif event.get("operation") == "device":
                message = f"{message} The app will retry the headset connection automatically."
            self.detail.setText(message)
            if event.get("operation") == "route" and self.feedback:
                self.feedback.play("error")
            self.status.setText("Could not switch microphone" if event.get("operation") == "route" else "Reconnecting to headset button…")
            if event.get("operation") == "configuration":
                self._request_service_stop()

    def closeEvent(self, event):
        if self.tray and self.tray.isVisible():
            self.hide()
            event.ignore()
            return
        if self.service:
            self._closing = True
            event.ignore()
            self._request_service_stop()
            return
        self._finish_close()
        event.accept()

    def _finish_close(self):
        if self.feedback:
            self.feedback.close()
        if self.tray:
            self.tray.hide()
        self.hide()
        if QApplication.instance():
            QApplication.instance().quit()

    def quit_app(self):
        self._closing = True
        if self.service:
            self._request_service_stop()
        else:
            self._finish_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nova microphone switch desktop app")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--safe-mode", action="store_true")
    parser.add_argument("--tray", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("Nova")
    app.setApplicationName("NovaMicSwitch")
    config = args.config_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    coordinator = None
    if not args.safe_mode:
        coordinator = InstanceCoordinator(config)
        command = "tray" if args.tray else "activate"
        if not coordinator.acquire_or_notify(command):
            return 0
    window = DesktopWindow(config, safe_mode=args.safe_mode)
    app.setQuitOnLastWindowClosed(not window.tray_available)
    if coordinator is not None:
        def activate_existing(command):
            if command == "activate":
                window.showNormal()
                window.raise_()
                window.activateWindow()
        coordinator.activation.connect(activate_existing)
        app.aboutToQuit.connect(coordinator.close)
    present_window(window, tray_requested=args.tray)
    if args.smoke_test:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(350, app.quit)
    result = app.exec()
    if coordinator is not None:
        coordinator.close()
    return result


def present_window(window: DesktopWindow, *, tray_requested: bool) -> None:
    settings = window.settings
    saved_pair_is_confirmed = bool(
        getattr(window, "config_existed", False)
        and settings.auto_start
        and settings.desktop_source
        and settings.headset_source
        and settings.desktop_source != settings.headset_source
        and not getattr(window, "safe_mode", False)
        and not getattr(window, "load_error", "")
    )
    launch_hidden = bool(
        window.tray_available
        and not getattr(window, "safe_mode", False)
        and (tray_requested or (saved_pair_is_confirmed and settings.start_minimized))
    )
    if launch_hidden:
        window.hide()
        if tray_requested and not saved_pair_is_confirmed:
            window.tray.showMessage("Setup needed", "Open Nova Mic Switch to choose your microphone pair.", QSystemTrayIcon.MessageIcon.Information, 5000)
    else:
        window.show()
        if (tray_requested or (saved_pair_is_confirmed and settings.start_minimized)) and not window.tray_available:
            window.detail.setText("System tray is unavailable, so the app opened as a normal window.")


if __name__ == "__main__":
    raise SystemExit(main())
