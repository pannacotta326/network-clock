# -*- coding: utf-8 -*-
"""
Windows 向けネットワーク時計: オンライン時は NTP、オフライン時はローカル時刻。
PyInstaller で exe 化: build_exe.bat 参照。
"""
from __future__ import annotations

import ctypes
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QLocale, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGraphicsDropShadowEffect,
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
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_RUN_VALUE_NAME = "Tokei_NetworkClock"

NTP_SERVER = "pool.ntp.org"
NTP_TIMEOUT = 3.0
ONLINE_CHECK_TIMEOUT = 2.0

WINDOW_WIDTH = 520
WINDOW_HEIGHT = 220

# assets フォルダに配置（クリックでその状態へ遷移するアイコン）
ASSET_TOP_ENABLE = "always_on_top_enable.png"  # 最前面オフのとき表示 → オンへ
ASSET_TOP_DISABLE = "always_on_top_disable.png"  # 最前面オンのとき表示 → オフへ


def resource_dir() -> Path:
    """PyInstaller 一ファイル時は実行ファイルのあるフォルダ、通常はスクリプト所在。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def windows_startup_command() -> str:
    """レジストリ Run に登録するコマンド行（パスに空白があれば引用符で囲む）。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable).resolve()
    script = Path(__file__).resolve()
    pythonw = exe.parent / "pythonw.exe"
    launcher = pythonw if pythonw.is_file() else exe
    return f'"{launcher}" "{script}"'


def get_windows_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             WIN_RUN_KEY, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        try:
            val, _ = winreg.QueryValueEx(key, WIN_RUN_VALUE_NAME)
        except FileNotFoundError:
            return False
        return bool(str(val).strip())
    finally:
        winreg.CloseKey(key)


def set_windows_autostart(enabled: bool) -> Optional[str]:
    """Windows ログオン時の自動起動を登録/解除。失敗時はエラーメッセージを返す。"""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            WIN_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
    except OSError as e:
        return str(e)
    try:
        if enabled:
            winreg.SetValueEx(key, WIN_RUN_VALUE_NAME, 0,
                              winreg.REG_SZ, windows_startup_command())
        else:
            try:
                winreg.DeleteValue(key, WIN_RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    except OSError as e:
        return str(e)
    finally:
        winreg.CloseKey(key)
    return None


def assets_dir() -> Path:
    """同梱アイコン。onefile ビルドでは --add-data で _MEIPASS/assets に展開される。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "assets"
    return resource_dir() / "assets"


def load_asset_icon(filename: str, fallback: QStyle.StandardPixmap) -> QIcon:
    path = assets_dir() / filename
    if path.is_file():
        pix = QPixmap(str(path))
        if not pix.isNull():
            return QIcon(pix)
    style = QApplication.instance()
    if style and isinstance(style, QApplication):
        st = style.style()
        if st:
            return st.standardIcon(fallback)
    return QIcon()


def _luminance_from_image_region(img: QImage, rect: QRect) -> float:
    """rect 内の画素から相対輝度（おおよそ 0〜1）を粗く推定。透過は無視に近い重み。"""
    r = rect.normalized().intersected(img.rect())
    if r.isEmpty():
        return 0.15
    total = 0.0
    weight = 0.0
    sx = max(1, r.width() // 14)
    sy = max(1, r.height() // 14)
    for y in range(r.top(), r.bottom(), sy):
        for x in range(r.left(), r.right(), sx):
            c = QColor(img.pixel(x, y))
            a = c.alphaF()
            if a < 0.04:
                continue
            lum = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
            total += lum * a
            weight += a
    if weight < 1e-6:
        return 0.15
    return total / weight


def _solid_luminance(html: str) -> float:
    c = QColor(html)
    return 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()


def _win_set_dwm_extend_frame(hwnd: int, extend: bool) -> None:
    """Windows DWM: クライアント領域を透過合成にする（Python / EXE 共通）。"""
    if sys.platform != "win32" or hwnd <= 0:
        return

    class _Margins(ctypes.Structure):
        _fields_ = [
            ("cxLeftWidth", ctypes.c_long),
            ("cxRightWidth", ctypes.c_long),
            ("cyTopHeight", ctypes.c_long),
            ("cyBottomHeight", ctypes.c_long),
        ]

    margins = _Margins(-1, -1, -1, -1) if extend else _Margins(0, 0, 0, 0)
    try:
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except OSError:
        pass


def is_online() -> bool:
    try:
        socket.create_connection(
            ("1.1.1.1", 443), timeout=ONLINE_CHECK_TIMEOUT)
        return True
    except OSError:
        try:
            socket.create_connection(
                ("8.8.8.8", 53), timeout=ONLINE_CHECK_TIMEOUT)
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


class _ShadowTextLabel(QLabel):
    """背景を塗らず、文字だけにシャドウを付けて描画するラベル。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        shadow_offset: tuple[int, int] = (2, 2),
        shadow_alpha: int = 200,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._shadow_offset = shadow_offset
        self._shadow_color = QColor(0, 0, 0, shadow_alpha)
        self._text_color = QColor(255, 255, 255)
        self._hover_color = QColor(0xCC, 0xE8, 0xFF)
        self._hovering = False

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovering = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovering = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        text = self.text()
        if not text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self.font())
        rect = self.rect()
        align = int(self.alignment())
        ox, oy = self._shadow_offset
        painter.setPen(self._shadow_color)
        painter.drawText(rect.translated(ox, oy), align, text)
        painter.setPen(
            self._hover_color if self._hovering else self._text_color)
        painter.drawText(rect, align, text)


class _PeekContainerWidget(QWidget):
    """コンパクト表示用コンテナ。背景は一切描画しない。"""

    def paintEvent(self, event) -> None:  # type: ignore[override]
        pass


class _ClockCentralWidget(QWidget):
    def __init__(self, window: "ClockWindow") -> None:
        super().__init__()
        self._window = window

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._window._peek_mode:
            return
        super().paintEvent(event)


class _ShadowArrowLabel(_ShadowTextLabel):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # type: ignore[override]
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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
        families = sorted(set(QFontDatabase.families()),
                          key=lambda s: s.lower())
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

        self._chk_autostart: Optional[QCheckBox] = None
        startup_box: Optional[QGroupBox] = None
        if sys.platform == "win32":
            startup_box = QGroupBox("起動")
            sl = QVBoxLayout(startup_box)
            self._chk_autostart = QCheckBox("Windows ログオン時に自動で起動する")
            self._chk_autostart.setChecked(get_windows_autostart_enabled())
            sl.addWidget(self._chk_autostart)

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
        if startup_box is not None:
            root.addWidget(startup_box)
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
        if self._chk_autostart is not None:
            err = set_windows_autostart(self._chk_autostart.isChecked())
            if err:
                QMessageBox.warning(
                    self,
                    "設定",
                    f"自動起動の登録を更新できませんでした。\n{err}",
                )


class ClockWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self._settings = QSettings(APP_ORG, APP_NAME)
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if self._settings.value("stays_on_top", False, type=bool):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle("")
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self._ntp_offset: Optional[float] = None
        self._bg_source = QPixmap()
        self._dragging_window = False
        self._drag_offset = QPoint()
        self._peek_mode = False
        self._peek_corner = "top-left"
        self._saved_geometry_before_peek = QRect()

        self._pool = ThreadPoolExecutor(max_workers=2)
        self._ntp_worker = NtpWorker(self._pool)
        self._ntp_worker.offset_ready.connect(self._on_ntp_offset)

        self._btn_stays_on_top = QPushButton()
        self._btn_stays_on_top.setFlat(True)
        self._btn_stays_on_top.setFixedSize(40, 40)
        self._btn_stays_on_top.setIconSize(QSize(32, 32))
        self._btn_stays_on_top.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_stays_on_top.clicked.connect(self._toggle_stays_on_top)
        self._btn_stays_on_top.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._btn_stays_on_top.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0px; }"
            "QPushButton:pressed { background: transparent; border: none; }"
        )
        self._toggle_icon_shadow = QGraphicsDropShadowEffect(
            self._btn_stays_on_top)
        self._btn_stays_on_top.setGraphicsEffect(self._toggle_icon_shadow)

        central = _ClockCentralWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)

        self._clock_area = QWidget()
        grid = QGridLayout(self._clock_area)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setRowStretch(0, 1)
        grid.setColumnStretch(0, 1)

        self._bg = QLabel()
        self._bg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bg.setSizePolicy(QSizePolicy.Policy.Ignored,
                               QSizePolicy.Policy.Ignored)
        self._bg.setMinimumSize(1, 1)

        self._time = QLabel("00:00:00")
        self._time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tf = QFont(str(self._settings.value("font_family", "Segoe UI")), 48)
        tf.setBold(True)
        self._time.setFont(tf)

        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub.setStyleSheet("color: #aaa; font-size: 12px;")

        self._overlay = QWidget()
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._overlay.installEventFilter(self)
        ov = QVBoxLayout(self._overlay)
        ov.setContentsMargins(16, 16, 16, 16)

        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self._btn_stays_on_top, 0,
                          Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        ov.addLayout(top_row)

        ov.addStretch(1)
        ov.addWidget(self._time, alignment=Qt.AlignmentFlag.AlignHCenter)
        ov.addWidget(self._sub, alignment=Qt.AlignmentFlag.AlignHCenter)
        ov.addStretch(2)

        grid.addWidget(self._bg, 0, 0)
        grid.addWidget(self._overlay, 0, 0)
        root.addWidget(self._clock_area, stretch=1)

        self._peek_widget = _PeekContainerWidget()
        self._peek_widget.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._peek_widget.setAutoFillBackground(False)
        peek_row = QHBoxLayout(self._peek_widget)
        peek_row.setContentsMargins(0, 0, 0, 0)
        peek_row.setSpacing(4)

        self._peek_arrow = _ShadowArrowLabel()
        self._peek_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow_f = QFont(
            str(self._settings.value("font_family", "Segoe UI")), 22)
        arrow_f.setBold(True)
        self._peek_arrow.setFont(arrow_f)
        self._peek_arrow.setText("▶")
        self._peek_arrow.clicked.connect(self._exit_peek_mode)

        self._peek_time = _ShadowTextLabel()
        self._peek_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        peek_tf = QFont(
            str(self._settings.value("font_family", "Segoe UI")), 28)
        peek_tf.setBold(True)
        self._peek_time.setFont(peek_tf)

        peek_row.addWidget(self._peek_arrow)
        peek_row.addWidget(self._peek_time)
        self._peek_widget.hide()
        root.addWidget(self._peek_widget)

        self._update_stays_on_top_button()
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
            self._tray.setIcon(style.standardIcon(
                QStyle.StandardPixmap.SP_ComputerIcon))
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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._overlay or self._peek_mode:
            return super().eventFilter(watched, event)

        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            if not isinstance(event, QMouseEvent) or event.button() != Qt.MouseButton.LeftButton:
                return False
            me = event
            child = self._overlay.childAt(me.position().toPoint())
            if child is self._btn_stays_on_top:
                return False
            wh = self.windowHandle()
            if wh is not None:
                wh.startSystemMove()
                return True
            self._dragging_window = True
            self._drag_offset = me.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return True
        if et == QEvent.Type.MouseMove:
            me = event
            if (
                isinstance(me, QMouseEvent)
                and self._dragging_window
                and me.buttons() & Qt.MouseButton.LeftButton
            ):
                self.move(me.globalPosition().toPoint() - self._drag_offset)
                return True
        if et == QEvent.Type.MouseButtonRelease:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                child = self._overlay.childAt(me.position().toPoint())
                if child is not self._btn_stays_on_top:
                    self._maybe_enter_peek_mode()
                self._dragging_window = False
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._peek_mode:
            return
        super().paintEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_bg_scaled()

    def _apply_win_peek_compositing(self, enable: bool) -> None:
        if not self._peek_mode and enable:
            return
        _win_set_dwm_extend_frame(int(self.winId()), enable)

    def _sync_win_peek_compositing(self) -> None:
        if self._peek_mode:
            self._apply_win_peek_compositing(True)

    def _screen_available_geometry(self) -> QRect | None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _set_widget_transparent(self, widget: QWidget, transparent: bool) -> None:
        widget.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, transparent)
        widget.setAutoFillBackground(not transparent)
        pal = widget.palette()
        if transparent:
            pal.setColor(widget.backgroundRole(), Qt.GlobalColor.transparent)
        else:
            pal.setColor(widget.backgroundRole(), QColor(0, 0, 0, 0))
        widget.setPalette(pal)

    def _apply_peek_transparency(self, enabled: bool) -> None:
        targets: list[QWidget | None] = [
            self,
            self.centralWidget(),
            self._peek_widget,
            self._peek_time,
            self._peek_arrow,
        ]
        for widget in targets:
            if widget is not None:
                self._set_widget_transparent(widget, enabled)
        if enabled:
            self.setStyleSheet("background: transparent;")
            cw = self.centralWidget()
            if cw:
                cw.setStyleSheet("background: transparent;")
        else:
            self.setStyleSheet("")
            cw = self.centralWidget()
            if cw:
                cw.setStyleSheet("")

    def _update_peek_label_sizes(self) -> None:
        time_fm = QFontMetrics(self._peek_time.font())
        arrow_fm = QFontMetrics(self._peek_arrow.font())
        self._peek_time.setFixedSize(
            time_fm.horizontalAdvance("88:88"), time_fm.height())
        self._peek_arrow.setFixedSize(
            arrow_fm.horizontalAdvance("▶"), arrow_fm.height())

    def _peek_window_size(self) -> QSize:
        time_fm = QFontMetrics(self._peek_time.font())
        arrow_fm = QFontMetrics(self._peek_arrow.font())
        time_w = time_fm.horizontalAdvance("88:88")
        arrow_w = arrow_fm.horizontalAdvance("▶")
        content_h = max(time_fm.height(), arrow_fm.height())
        shadow_pad = 4
        spacing = 4
        return QSize(
            arrow_w + time_w + spacing + shadow_pad * 2,
            content_h + shadow_pad * 2,
        )

    def _layout_peek_for_corner(self, corner: str) -> None:
        left_side = corner.endswith("left")
        self._peek_arrow.setText("▶" if left_side else "◀")
        layout = self._peek_widget.layout()
        if not isinstance(layout, QHBoxLayout):
            return
        layout.removeWidget(self._peek_arrow)
        layout.removeWidget(self._peek_time)
        if left_side:
            layout.insertWidget(0, self._peek_arrow)
            layout.insertWidget(1, self._peek_time)
        else:
            layout.insertWidget(0, self._peek_time)
            layout.insertWidget(1, self._peek_arrow)

    def _position_peek_at_corner(self, corner: str) -> None:
        avail = self._screen_available_geometry()
        if avail is None:
            return
        size = self._peek_window_size()
        margin = 8
        if corner == "top-left":
            pos = QPoint(avail.left() + margin, avail.top() + margin)
        elif corner == "top-right":
            pos = QPoint(avail.right() - size.width() +
                         1 - margin, avail.top() + margin)
        elif corner == "bottom-left":
            pos = QPoint(avail.left() + margin, avail.bottom() -
                         size.height() + 1 - margin)
        else:
            pos = QPoint(
                avail.right() - size.width() + 1 - margin,
                avail.bottom() - size.height() + 1 - margin,
            )
        self.setGeometry(pos.x(), pos.y(), size.width(), size.height())

    def _pick_peek_corner(
        self,
        avail: QRect,
        win: QRect,
        hidden_left: int,
        hidden_right: int,
        off_top: bool,
        off_bottom: bool,
    ) -> str:
        cy = win.center().y()
        top = cy < avail.center().y()
        on_left = hidden_left >= hidden_right
        if on_left:
            return "top-left" if (off_top or top) else "bottom-left"
        return "top-right" if (off_top or top) else "bottom-right"

    def _maybe_enter_peek_mode(self) -> bool:
        avail = self._screen_available_geometry()
        if avail is None:
            return False
        win = self.frameGeometry()
        win_w = win.width()
        if win_w <= 0:
            return False

        hidden_left = max(0, avail.left() - win.left())
        hidden_right = max(0, win.right() - avail.right())
        half = win_w / 2
        if hidden_left <= half and hidden_right <= half:
            return False

        off_top = win.top() < avail.top()
        off_bottom = win.bottom() > avail.bottom()
        corner = self._pick_peek_corner(
            avail, win, hidden_left, hidden_right, off_top, off_bottom)
        self._enter_peek_mode(corner)
        return True

    def _enter_peek_mode(self, corner: str) -> None:
        self._saved_geometry_before_peek = self.geometry()
        self._peek_corner = corner
        self._peek_mode = True
        self._dragging_window = False

        self._clock_area.hide()
        self._peek_widget.show()
        self._layout_peek_for_corner(corner)
        self._update_peek_label_sizes()
        self._apply_peek_transparency(True)

        size = self._peek_window_size()
        self.setFixedSize(size.width(), size.height())
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        self._position_peek_at_corner(corner)
        self.show()
        QTimer.singleShot(0, self._sync_win_peek_compositing)

    def _exit_peek_mode(self) -> None:
        if not self._peek_mode:
            return
        self._peek_mode = False
        self._dragging_window = False

        self._apply_win_peek_compositing(False)
        self._peek_widget.hide()
        self._clock_area.show()
        self._apply_peek_transparency(False)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, False)

        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)
        if not self._saved_geometry_before_peek.isNull():
            self.setGeometry(self._saved_geometry_before_peek)
        self.show()
        self._update_stays_on_top_button()

    def _sample_background_luminance_button_zone(self) -> float:
        """トグル付近（右上）の背景輝度。画像が無いときはウィジェット背景色に相当する値。"""
        cw = self._clock_area.width()
        ch = self._clock_area.height()
        if cw < 8 or ch < 8:
            return _solid_luminance("#1a1a2e") if self._bg_source.isNull() else 0.12
        if self._bg_source.isNull():
            return _solid_luminance("#1a1a2e")
        scaled = self._bg_source.scaled(
            cw,
            ch,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        img = scaled.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        rw = max(8, cw * 22 // 100)
        rh = max(8, ch * 32 // 100)
        rect = QRect(cw - rw, 0, rw, rh)
        return _luminance_from_image_region(img, rect)

    def _refresh_toggle_shadow(self) -> None:
        lum = self._sample_background_luminance_button_zone()
        if lum < 0.45:
            self._toggle_icon_shadow.setColor(QColor(255, 255, 255, 105))
            self._toggle_icon_shadow.setBlurRadius(18)
            self._toggle_icon_shadow.setOffset(0, 0)
        else:
            self._toggle_icon_shadow.setColor(QColor(0, 0, 0, 150))
            self._toggle_icon_shadow.setBlurRadius(12)
            self._toggle_icon_shadow.setOffset(0, 2)

    def _update_stays_on_top_button(self) -> None:
        on = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if on:
            icon = load_asset_icon(
                ASSET_TOP_DISABLE, QStyle.StandardPixmap.SP_ArrowDown)
            self._btn_stays_on_top.setIcon(icon)
            self._btn_stays_on_top.setToolTip("最前面表示をオフ")
        else:
            icon = load_asset_icon(
                ASSET_TOP_ENABLE, QStyle.StandardPixmap.SP_ArrowUp)
            self._btn_stays_on_top.setIcon(icon)
            self._btn_stays_on_top.setToolTip("最前面表示をオン")
        self._refresh_toggle_shadow()

    def _toggle_stays_on_top(self) -> None:
        new_on = not bool(self.windowFlags() &
                          Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, new_on)
        self._settings.setValue("stays_on_top", new_on)
        self.show()
        self._update_stays_on_top_button()

    def _update_bg_scaled(self) -> None:
        if self._bg_source.isNull():
            self._refresh_toggle_shadow()
            return
        self._bg.setPixmap(
            self._bg_source.scaled(
                self._clock_area.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._refresh_toggle_shadow()

    def _apply_background(self) -> None:
        path = str(self._settings.value("background_path", "") or "").strip()
        if path and Path(path).is_file():
            pm = QPixmap(path)
            if not pm.isNull():
                self._bg_source = pm
                self._bg.setStyleSheet("background-color: #111;")
                self._time.setStyleSheet(
                    "color: #fff; background: rgba(0,0,0,120); padding: 8px; border-radius: 8px;")
                self._sub.setStyleSheet(
                    "color: #ddd; background: rgba(0,0,0,80); padding: 4px; border-radius: 4px;")
                self._update_bg_scaled()
                return
        self._bg_source = QPixmap()
        self._bg.clear()
        self._bg.setStyleSheet("background-color: #1a1a2e;")
        self._time.setStyleSheet("color: #eaeaea;")
        self._sub.setStyleSheet("color: #aaa;")
        self._refresh_toggle_shadow()

    def _reload_font(self) -> None:
        fam = str(self._settings.value("font_family", "Segoe UI"))
        f = QFont(fam, 48)
        f.setBold(True)
        self._time.setFont(f)
        pf = QFont(fam, 28)
        pf.setBold(True)
        self._peek_time.setFont(pf)
        af = QFont(fam, 22)
        af.setBold(True)
        self._peek_arrow.setFont(af)
        if self._peek_mode:
            self._update_peek_label_sizes()
            size = self._peek_window_size()
            self.setFixedSize(size.width(), size.height())
            self._position_peek_at_corner(self._peek_corner)

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
        self._peek_time.setText(loc.toString(dt, "HH:mm"))
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
            if self._peek_mode:
                self._exit_peek_mode()
            else:
                self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)
                self.show()
            self.raise_()
            self.activateWindow()
            self._update_stays_on_top_button()

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
    QLocale.setDefault(
        QLocale(QLocale.Language.Japanese, QLocale.Country.Japan))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    # タスクバー等に表示される表示名（空に近づける）
    app.setApplicationDisplayName("")
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
