# -*- coding: utf-8 -*-
"""
Windows 向けネットワーク時計: オンライン時は NTP、オフライン時はローカル時刻。
PyInstaller で exe 化: build_exe.bat 参照。
"""
from __future__ import annotations

import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QLocale, QSettings, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QAction, QFont, QFontDatabase, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

try:
    import ntplib
except ImportError:  # 開発時のフォールバック
    ntplib = None  # type: ignore


APP_ORG = "Tokei"
APP_NAME = "NetworkClock"
NTP_SERVER = "pool.ntp.org"
NTP_TIMEOUT = 3.0
ONLINE_CHECK_TIMEOUT = 2.0


def resource_dir() -> Path:
    """PyInstaller 一ファイル時は実行ファイルのあるフォルダ、通常はスクリプト所在。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def is_online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=ONLINE_CHECK_TIMEOUT)
        return True
    except OSError:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=ONLINE_CHECK_TIMEOUT)
            return True
        except OSError:
            return False


def fetch_ntp_offset_seconds() -> Optional[float]:
    if ntplib is None:
        return None
    try:
        client = ntplib.NTPClient()
        resp = client.request(NTP_SERVER, version=3, timeout=NTP_TIMEOUT)
        return float(resp.offset)
    except Exception:
        return None


class NtpWorker(QObject):
    """スレッドプールで NTP を取得し、完了をメインスレッドへ通知。"""

    offset_ready = Signal(object)  # Optional[float]

    def __init__(self, pool: ThreadPoolExecutor) -> None:
        super().__init__()
        self._pool = pool

    def request_sync(self) -> None:
        fut = self._pool.submit(self._job)

        def poll() -> None:
            if not fut.done():
                QTimer.singleShot(80, poll)
                return
            try:
                off = fut.result(timeout=0)
            except Exception:
                off = None
            self.offset_ready.emit(off)

        QTimer.singleShot(0, poll)

    @staticmethod
    def _job() -> Optional[float]:
        if not is_online():
            return None
        return fetch_ntp_offset_seconds()


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(420)

        self._settings = QSettings(APP_ORG, APP_NAME)
        self._font_combo = QComboBox()
        self._font_combo.setMaxVisibleItems(20)
        families = sorted(set(QFontDatabase.families()), key=lambda s: s.lower())
        self._font_combo.addItems(families)

        saved_font = self._settings.value("font_family", "Segoe UI")
        idx = self._font_combo.findText(str(saved_font))
        if idx >= 0:
            self._font_combo.setCurrentIndex(idx)

        self._bg_label = QLabel()
        self._bg_path = str(self._settings.value("background_path", "") or "")
        self._refresh_bg_preview()

        btn_bg = QPushButton("背景画像を選択…")
        btn_bg.clicked.connect(self._pick_background)

        btn_clear_bg = QPushButton("背景をクリア")
        btn_clear_bg.clicked.connect(self._clear_background)

        g = QGroupBox("表示")
        gl = QFormLayout(g)
        gl.addRow("フォント:", self._font_combo)
        gl.addRow(btn_bg)
        gl.addRow(btn_clear_bg)
        gl.addRow("プレビュー:", self._bg_label)

        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("キャンセル")
        btn_cancel.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn_ok)
        row.addWidget(btn_cancel)

        root = QVBoxLayout(self)
        root.addWidget(g)
        root.addLayout(row)

    def _refresh_bg_preview(self) -> None:
        p = self._bg_path.strip()
        if p and Path(p).is_file():
            pm = QPixmap(p)
            if not pm.isNull():
                self._bg_label.setPixmap(
                    pm.scaled(
                        320,
                        180,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self._bg_label.setText("")
                return
        self._bg_label.clear()
        self._bg_label.setText("（なし）")

    def _pick_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "背景画像を選択",
            str(resource_dir()),
            "画像 (*.png *.jpg *.jpeg *.bmp *.webp);;すべて (*.*)",
        )
        if path:
            self._bg_path = path
            self._refresh_bg_preview()

    def _clear_background(self) -> None:
        self._bg_path = ""
        self._refresh_bg_preview()

    def save_to_settings(self) -> None:
        self._settings.setValue("font_family", self._font_combo.currentText())
        self._settings.setValue("background_path", self._bg_path)


class ClockWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ネットワーク時計")
        self.resize(520, 220)
        self.setMinimumSize(360, 160)

        self._settings = QSettings(APP_ORG, APP_NAME)
        self._ntp_offset: Optional[float] = None
        self._bg_source = QPixmap()

        self._pool = ThreadPoolExecutor(max_workers=2)
        self._ntp_worker = NtpWorker(self._pool)
        self._ntp_worker.offset_ready.connect(self._on_ntp_offset)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)

        top_bar = QHBoxLayout()
        self._toggle_top = QCheckBox("最前面・全画面")
        self._toggle_top.setToolTip("ON: 常に最前面かつ全画面表示 / OFF: 通常ウィンドウ")
        self._toggle_top.toggled.connect(self._on_toggle_foreground_fullscreen)
        top_bar.addWidget(self._toggle_top)
        top_bar.addStretch()
        root.addLayout(top_bar)

        self._clock_area = QWidget()
        grid = QGridLayout(self._clock_area)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setRowStretch(0, 1)
        grid.setColumnStretch(0, 1)

        self._bg = QLabel()
        self._bg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bg.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._bg.setMinimumSize(1, 1)

        self._time = QLabel("00:00:00")
        self._time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tf = QFont(str(self._settings.value("font_family", "Segoe UI")), 48)
        tf.setBold(True)
        self._time.setFont(tf)

        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub.setStyleSheet("color: #aaa; font-size: 12px;")

        overlay = QWidget()
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        ov = QVBoxLayout(overlay)
        ov.setContentsMargins(16, 16, 16, 16)
        ov.addStretch(1)
        ov.addWidget(self._time, alignment=Qt.AlignmentFlag.AlignHCenter)
        ov.addWidget(self._sub, alignment=Qt.AlignmentFlag.AlignHCenter)
        ov.addStretch(2)

        grid.addWidget(self._bg, 0, 0)
        grid.addWidget(overlay, 0, 0)
        root.addWidget(self._clock_area, stretch=1)

        self._apply_background()

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(250)

        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._request_ntp)
        self._sync_timer.start(60_000)
        QTimer.singleShot(500, self._request_ntp)

        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("ネットワーク時計")
        style = self.style()
        if style:
            self._tray.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        tray_menu = QMenu()
        act_settings = QAction("設定…", self)
        act_settings.triggered.connect(self._open_settings)
        act_quit = QAction("終了", self)
        act_quit.triggered.connect(QApplication.quit)
        tray_menu.addAction(act_settings)
        tray_menu.addSeparator()
        tray_menu.addAction(act_quit)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_bg_scaled()

    def _update_bg_scaled(self) -> None:
        if self._bg_source.isNull():
            return
        self._bg.setPixmap(
            self._bg_source.scaled(
                self._clock_area.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _apply_background(self) -> None:
        path = str(self._settings.value("background_path", "") or "").strip()
        if path and Path(path).is_file():
            pm = QPixmap(path)
            if not pm.isNull():
                self._bg_source = pm
                self._bg.setStyleSheet("background-color: #111;")
                self._time.setStyleSheet("color: #fff; background: rgba(0,0,0,120); padding: 8px; border-radius: 8px;")
                self._sub.setStyleSheet("color: #ddd; background: rgba(0,0,0,80); padding: 4px; border-radius: 4px;")
                self._update_bg_scaled()
                return
        self._bg_source = QPixmap()
        self._bg.clear()
        self._bg.setStyleSheet("background-color: #1a1a2e;")
        self._time.setStyleSheet("color: #eaeaea;")
        self._sub.setStyleSheet("color: #aaa;")

    def _reload_font(self) -> None:
        fam = str(self._settings.value("font_family", "Segoe UI"))
        f = QFont(fam, 48)
        f.setBold(True)
        self._time.setFont(f)

    def _on_toggle_foreground_fullscreen(self, on: bool) -> None:
        if on:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            self.showFullScreen()
        else:
            self.showNormal()
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            self.resize(520, 220)
            self.show()

    def _request_ntp(self) -> None:
        self._ntp_worker.request_sync()

    def _on_ntp_offset(self, offset: object) -> None:
        if offset is None:
            self._ntp_offset = None
        else:
            self._ntp_offset = float(offset)

    def _tick_clock(self) -> None:
        now_ts = time.time()
        if self._ntp_offset is not None:
            dt = datetime.fromtimestamp(now_ts + self._ntp_offset)
            src = "ネットワーク時刻 (NTP)"
        else:
            dt = datetime.now()
            src = "本体時刻"

        loc = QLocale(QLocale.Language.Japanese, QLocale.Country.Japan)
        self._time.setText(loc.toString(dt, "HH:mm:ss"))
        date_part = loc.toString(dt, "yyyy/MM/dd (ddd)")
        self._sub.setText(f"{date_part} ・ {src}")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.save_to_settings()
            self._reload_font()
            self._apply_background()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if self._tray.isVisible():
            self.hide()
            self._tray.showMessage(
                "ネットワーク時計",
                "タスクトレイで動作中です。終了はトレイメニューから。",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
            event.ignore()
        else:
            event.accept()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def main() -> int:
    QLocale.setDefault(QLocale(QLocale.Language.Japanese, QLocale.Country.Japan))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "エラー", "システムトレイが利用できません。")
        return 1

    w = ClockWindow()
    w.show()

    code = app.exec()
    w.shutdown()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
