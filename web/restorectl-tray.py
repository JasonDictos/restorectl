#!/usr/bin/env python3
"""restorectl-tray — KDE/Qt system tray monitor for restorectl backups.

Shows backup state at a glance, streams live progress while a backup runs,
and gets you to the web UI in one click. Uses PySide6, which ships with
Fedora KDE, so there is nothing to pip install.

  restorectl tray            run it
  restorectl tray --autostart install a KDE autostart entry

MIT licensed. https://github.com/JasonDictos/restorectl
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import subprocess
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QMenu,
                               QPlainTextEdit, QPushButton, QSystemTrayIcon,
                               QVBoxLayout)

SERVICE = "system-backup.service"
TIMER = "system-backup.timer"
LOGDIR = "/var/log/system-backup"
WEB_URL = os.environ.get("RESTORECTL_WEB_URL", "http://home/restorectl/")
POLL_MS = 5000

IDLE_OK, RUNNING, FAILED, UNKNOWN = range(4)
COLOURS = {
    IDLE_OK: ("#2f9e5f", "backups healthy"),
    RUNNING: ("#2f7fd0", "backup running"),
    FAILED:  ("#c0392b", "backup FAILED"),
    UNKNOWN: ("#888888", "state unknown"),
}


def sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def make_icon(state, pct=None):
    """Draw the tray icon: a filled disc, with an arc showing progress while
    a backup runs so the tray itself is the progress indicator."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    colour = QColor(COLOURS[state][0])
    if state == RUNNING and pct is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3a3a3a"))
        p.drawEllipse(4, 4, size - 8, size - 8)
        p.setBrush(colour)
        # Qt angles are in 1/16 degree, counter-clockwise from 3 o'clock.
        p.drawPie(4, 4, size - 8, size - 8, 90 * 16, int(-360 * 16 * pct / 100))
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawEllipse(6, 6, size - 12, size - 12)
    p.end()
    return QIcon(pm)


class LogWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("restorectl — backup detail")
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setTextFormat(Qt.RichText)
        lay.addWidget(self.head)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setStyleSheet("font-family:monospace;font-size:11px")
        lay.addWidget(self.body)
        btn = QPushButton("Open web UI")
        btn.clicked.connect(lambda: subprocess.Popen(["xdg-open", WEB_URL]))
        lay.addWidget(btn)

    def refresh(self, header, text):
        self.head.setText(header)
        # Keep the scrollback pinned to the bottom while a backup streams.
        at_end = self.body.verticalScrollBar().value() == \
            self.body.verticalScrollBar().maximum()
        self.body.setPlainText(text)
        if at_end:
            self.body.verticalScrollBar().setValue(
                self.body.verticalScrollBar().maximum())


class Tray:
    def __init__(self, app):
        self.app = app
        self.state = UNKNOWN
        self.pct = None
        self.tray = QSystemTrayIcon(make_icon(UNKNOWN))
        self.win = LogWindow()

        m = QMenu()
        self.act_status = QAction("checking…")
        self.act_status.setEnabled(False)
        m.addAction(self.act_status)
        m.addSeparator()

        a_detail = QAction("Show detail / live log")
        a_detail.triggered.connect(self.show_detail)
        m.addAction(a_detail)

        a_web = QAction("Open backup browser (web)")
        a_web.triggered.connect(lambda: subprocess.Popen(["xdg-open", WEB_URL]))
        m.addAction(a_web)

        a_run = QAction("Run backup now")
        a_run.triggered.connect(self.run_now)
        m.addAction(a_run)

        a_wtf = QAction("Run restorectl wtf in a terminal")
        a_wtf.triggered.connect(self.open_wtf)
        m.addAction(a_wtf)

        m.addSeparator()
        a_quit = QAction("Quit")
        a_quit.triggered.connect(app.quit)
        m.addAction(a_quit)

        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_detail() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()

    # ---------------------------------------------------------------- data
    def latest_log(self):
        logs = sorted(glob.glob(os.path.join(LOGDIR, "*.log")))
        return logs[-1] if logs else None

    def progress(self):
        """Parse restic's --json progress stream for percent complete."""
        logs = sorted(glob.glob(os.path.join(LOGDIR, "*.log.json")))
        if not logs:
            return None, ""
        try:
            with open(logs[-1], "rb") as fh:
                # Only the tail matters; these files get large.
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 65536))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None, ""
        for line in reversed(lines):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("message_type") == "status":
                pct = float(m.get("percent_done", 0)) * 100
                cur = (m.get("current_files") or [""])[0]
                return pct, cur
            if m.get("message_type") == "summary":
                return 100.0, "finishing"
        return None, ""

    def poll(self):
        active = sh(f"systemctl is-active {SERVICE}")
        result = sh(f"systemctl show {SERVICE} -p Result --value")
        enabled = sh(f"systemctl is-enabled {TIMER}")
        nxt = sh(f"systemctl show {TIMER} -p NextElapseUSecRealtime --value")

        if active in ("activating", "active"):
            self.state = RUNNING
            self.pct, cur = self.progress()
            label = f"backup running — {self.pct:.0f}%" if self.pct else "backup running"
            tip = f"{label}\n{cur[:70]}"
        elif result and result != "success":
            self.state = FAILED
            self.pct = None
            label, tip = f"LAST BACKUP FAILED ({result})", f"result: {result}"
        elif enabled != "enabled":
            self.state = FAILED
            self.pct = None
            label = tip = "nightly timer is DISABLED"
        else:
            self.state = IDLE_OK
            self.pct = None
            label = "backups healthy"
            tip = f"next run: {nxt or 'unknown'}"

        self.act_status.setText(label)
        self.tray.setIcon(make_icon(self.state, self.pct))
        self.tray.setToolTip(f"restorectl — {tip}")

        if self.win.isVisible():
            self.refresh_detail()

    # ------------------------------------------------------------- actions
    def refresh_detail(self):
        nxt = sh(f"systemctl show {TIMER} -p NextElapseUSecRealtime --value")
        res = sh(f"systemctl show {SERVICE} -p Result --value") or "never run"
        colour, _ = COLOURS[self.state]
        header = (f"<b style='color:{colour}'>{COLOURS[self.state][1]}</b>"
                  f" &nbsp; last result: <b>{res}</b>"
                  f" &nbsp; next: <b>{nxt or 'unknown'}</b>")
        log = self.latest_log()
        text = ""
        if log:
            try:
                with open(log, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - 40000))
                    text = fh.read().decode("utf-8", "replace")
            except OSError as exc:
                text = f"(cannot read {log}: {exc})"
        if self.state == RUNNING:
            pct, cur = self.progress()
            if pct is not None:
                text += f"\n\n--- live ---\n{pct:.1f}%  {cur}"
        self.win.refresh(header, text or "(no log yet)")

    def show_detail(self):
        self.refresh_detail()
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def run_now(self):
        subprocess.Popen(["pkexec", "systemctl", "start", SERVICE])
        QTimer.singleShot(1500, self.poll)

    def open_wtf(self):
        for term in ("qterminal", "konsole", "xterm"):
            if subprocess.run(["which", term], capture_output=True).returncode == 0:
                subprocess.Popen([term, "-e", "bash", "-c",
                                  "restorectl wtf; echo; read -p 'enter to close'"])
                return


AUTOSTART = """[Desktop Entry]
Type=Application
Name=restorectl backup monitor
Exec={exe} tray
Icon=drive-harddisk
X-KDE-autostart-phase=2
NoDisplay=false
"""


def main():
    if "--autostart" in sys.argv:
        path = os.path.expanduser("~/.config/autostart/restorectl-tray.desktop")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(AUTOSTART.format(exe=shutil_which()))
        print(f"autostart installed: {path}")
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit("no system tray available")
    Tray(app)
    sys.exit(app.exec())


def shutil_which():
    import shutil
    return shutil.which("restorectl") or "/usr/local/bin/restorectl"


if __name__ == "__main__":
    main()
