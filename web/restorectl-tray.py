#!/usr/bin/env python3
"""restorectl-tray — KDE/Qt system tray monitor for restorectl backups.

Shows backup state at a glance, streams live progress while a backup runs,
and gets you to the web UI in one click. Uses PySide6, which ships with
Fedora KDE, so there is nothing to pip install.

  restorectl tray             run it
  restorectl tray --autostart install a KDE autostart entry

MIT licensed. https://github.com/JasonDictos/restorectl
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QSystemTrayIcon, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

SERVICE = "system-backup.service"
TIMER = "system-backup.timer"
LOGDIR = "/var/log/system-backup"
# Port 8088 is the standalone nginx server block restorectl ships; the old
# path-based URL (http://home/restorectl/) does not exist.
WEB_URL = os.environ.get("RESTORECTL_WEB_URL", "http://home:8088/")
POLL_MS = 4000

IDLE_OK, RUNNING, FAILED, UNKNOWN = range(4)
COLOURS = {
    IDLE_OK: ("#2f9e5f", "backups healthy"),
    RUNNING: ("#2f7fd0", "backup running"),
    FAILED:  ("#c0392b", "backup FAILED"),
    UNKNOWN: ("#888888", "state unknown"),
}


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"


def make_icon(state, pct=None):
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    colour = QColor(COLOURS[state][0])
    p.setPen(Qt.NoPen)
    if state == RUNNING and pct is not None:
        p.setBrush(QColor("#3a3a3a"))
        p.drawEllipse(4, 4, size - 8, size - 8)
        p.setBrush(colour)
        p.drawPie(4, 4, size - 8, size - 8, 90 * 16, int(-360 * 16 * pct / 100))
    else:
        p.setBrush(colour)
        p.drawEllipse(6, 6, size - 12, size - 12)
    p.end()
    return QIcon(pm)


class Tile(QFrame):
    """One stat: big value, small caption."""

    def __init__(self, caption):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame{border:1px solid palette(mid);border-radius:6px;padding:6px}")
        lay = QVBoxLayout(self)
        lay.setSpacing(1)
        lay.setContentsMargins(8, 6, 8, 6)
        self.value = QLabel("—")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        self.value.setFont(f)
        cap = QLabel(caption.upper())
        cf = QFont()
        cf.setPointSize(7)
        cap.setFont(cf)
        cap.setStyleSheet("color:palette(mid)")
        lay.addWidget(self.value)
        lay.addWidget(cap)

    def set(self, v):
        self.value.setText(str(v))


class Window(QDialog):
    def __init__(self, tray):
        super().__init__()
        self.tray = tray
        self.setWindowTitle("restorectl")
        self.resize(880, 600)
        root = QVBoxLayout(self)

        # ---- header: status + progress -------------------------------
        self.badge = QLabel()
        bf = QFont()
        bf.setPointSize(13)
        bf.setBold(True)
        self.badge.setFont(bf)
        self.sub = QLabel()
        self.sub.setStyleSheet("color:palette(mid)")
        root.addWidget(self.badge)
        root.addWidget(self.sub)

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setMinimumHeight(20)
        self.bar.hide()
        root.addWidget(self.bar)
        self.curfile = QLabel()
        self.curfile.setStyleSheet("color:palette(mid);font-family:monospace;font-size:10px")
        self.curfile.hide()
        root.addWidget(self.curfile)

        # ---- stat tiles ----------------------------------------------
        tiles = QHBoxLayout()
        self.t_files = Tile("files in snapshot")
        self.t_added = Tile("stored last run")
        self.t_repo = Tile("repository size")
        self.t_snaps = Tile("snapshots kept")
        self.t_next = Tile("next run")
        for t in (self.t_files, self.t_added, self.t_repo, self.t_snaps, self.t_next):
            tiles.addWidget(t)
        root.addLayout(tiles)

        # ---- tabs: snapshots / log -----------------------------------
        self.tabs = QTabWidget()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["when", "id", "size", "paths"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tabs.addTab(self.table, "Snapshots")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family:monospace;font-size:11px")
        self.tabs.addTab(self.log, "Log")
        root.addWidget(self.tabs)

        # ---- actions: several buttons, plural ------------------------
        bar = QHBoxLayout()

        def btn(label, slot, tip=""):
            b = QPushButton(label)
            b.clicked.connect(slot)
            if tip:
                b.setToolTip(tip)
            bar.addWidget(b)
            return b

        btn("Browse backups ↗", self.open_web,
            f"Open the snapshot browser at {WEB_URL}")
        self.b_run = btn("Back up now", self.run_now,
                         "Start system-backup.service immediately")
        btn("Check posture", self.run_wtf,
            "Run restorectl wtf and show the result here")
        btn("Restore a file…", self.open_restore,
            "Open the browser at this snapshot to download a file")
        btn("Open log folder", self.open_logs, LOGDIR)
        bar.addStretch(1)
        btn("Close", self.hide)
        root.addLayout(bar)

        self.snaps_cache = []

    # ------------------------------------------------------------ actions
    def open_web(self):
        subprocess.Popen(["xdg-open", WEB_URL])

    def open_restore(self):
        host = os.uname().nodename.split(".")[0]
        url = WEB_URL.rstrip("/") + f"/?host={host}"
        subprocess.Popen(["xdg-open", url])

    def open_logs(self):
        subprocess.Popen(["xdg-open", LOGDIR])

    def run_now(self):
        self.b_run.setEnabled(False)
        QProcess.startDetached("pkexec", ["systemctl", "start", SERVICE])
        QTimer.singleShot(3000, lambda: self.b_run.setEnabled(True))
        QTimer.singleShot(1500, self.tray.poll)

    def run_wtf(self):
        self.tabs.setCurrentWidget(self.log)
        self.log.setPlainText("running restorectl wtf …\n")
        exe = shutil.which("restorectl") or "/usr/local/bin/restorectl"
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)

        def done():
            raw = bytes(proc.readAll()).decode("utf-8", "replace")
            # Strip ANSI so it renders as text, not escape soup.
            import re
            self.log.setPlainText(re.sub(r"\x1b\[[0-9;]*m", "", raw))

        proc.finished.connect(done)
        proc.start(exe, ["wtf"])


class Tray:
    def __init__(self, app):
        self.app = app
        self.state = UNKNOWN
        self.pct = None
        self.tray = QSystemTrayIcon(make_icon(UNKNOWN))
        self.win = Window(self)

        m = QMenu()
        self.act_status = QAction("checking…")
        self.act_status.setEnabled(False)
        m.addAction(self.act_status)
        m.addSeparator()
        a_open = QAction("Open restorectl…")
        a_open.triggered.connect(self.show_window)
        m.addAction(a_open)
        a_web = QAction("Browse backups (web)")
        a_web.triggered.connect(self.win.open_web)
        m.addAction(a_web)
        a_run = QAction("Back up now")
        a_run.triggered.connect(self.win.run_now)
        m.addAction(a_run)
        m.addSeparator()
        a_quit = QAction("Quit")
        a_quit.triggered.connect(app.quit)
        m.addAction(a_quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()
        QTimer.singleShot(500, self.load_snapshots)

    # ---------------------------------------------------------------- data
    def progress(self):
        js = sorted(glob.glob(os.path.join(LOGDIR, "*.log.json")))
        if not js:
            return None, ""
        try:
            with open(js[-1], "rb") as fh:
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
                return float(m.get("percent_done", 0)) * 100, \
                    (m.get("current_files") or [""])[0]
            if m.get("message_type") == "summary":
                return 100.0, "finalising"
        return None, ""

    def last_summary(self):
        js = sorted(glob.glob(os.path.join(LOGDIR, "*.log.json")))
        for path in reversed(js[-3:]):
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - 32768))
                    for line in reversed(fh.read().decode("utf-8", "replace").splitlines()):
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            m = json.loads(line)
                        except ValueError:
                            continue
                        if m.get("message_type") == "summary":
                            return m
            except OSError:
                continue
        return {}

    def load_snapshots(self):
        exe = shutil.which("restorectl") or "/usr/local/bin/restorectl"
        raw = sh(f"{exe} --json snapshots -n 40", timeout=60)
        try:
            snaps = json.loads(raw)
        except ValueError:
            return
        snaps.sort(key=lambda s: s.get("time", ""), reverse=True)
        t = self.win.table
        t.setRowCount(len(snaps))
        for r, s in enumerate(snaps):
            when = (s.get("time") or "")[:19].replace("T", "  ")
            t.setItem(r, 0, QTableWidgetItem(when))
            t.setItem(r, 1, QTableWidgetItem(s.get("short_id", "")))
            t.setItem(r, 2, QTableWidgetItem(
                human(s.get("summary", {}).get("total_bytes_processed"))))
            t.setItem(r, 3, QTableWidgetItem(" ".join(s.get("paths", []))))
        t.resizeColumnsToContents()
        self.win.t_snaps.set(len(snaps))

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
            self.state, self.pct = FAILED, None
            label = tip = f"LAST BACKUP FAILED ({result})"
        elif enabled != "enabled":
            self.state, self.pct = FAILED, None
            label = tip = "nightly timer is DISABLED"
        else:
            self.state, self.pct = IDLE_OK, None
            label, tip = "backups healthy", f"next run: {nxt or 'unknown'}"

        self.act_status.setText(label)
        self.tray.setIcon(make_icon(self.state, self.pct))
        self.tray.setToolTip(f"restorectl — {tip}")

        if self.win.isVisible():
            self.refresh_window(result, nxt)

    def refresh_window(self, result, nxt):
        w = self.win
        colour, text = COLOURS[self.state]
        w.badge.setText(text)
        w.badge.setStyleSheet(f"color:{colour}")
        w.sub.setText(f"last result: {result or 'never run'}     "
                      f"repo: {sh('restorectl repo 2>/dev/null | head -1') or '—'}")

        if self.state == RUNNING and self.pct is not None:
            pct, cur = self.progress()
            w.bar.show()
            w.bar.setValue(int(pct or 0))
            w.bar.setFormat(f"{pct:.1f}%")
            w.curfile.setText(cur[:110])
            w.curfile.show()
        else:
            w.bar.hide()
            w.curfile.hide()

        s = self.last_summary()
        w.t_files.set(f"{s.get('total_files_processed', 0):,}" if s else "—")
        w.t_added.set(human(s.get("data_added")) if s else "—")
        w.t_next.set((nxt or "—").replace("PDT", "").replace("PST", "").strip()[-8:] or "—")
        stats = sh("restorectl stats 2>/dev/null | grep -m1 'Total Size'")
        w.t_repo.set(stats.split(":", 1)[1].strip() if ":" in stats else "—")

        logs = sorted(glob.glob(os.path.join(LOGDIR, "*.log")))
        if logs:
            try:
                with open(logs[-1], "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - 40000))
                    at_end = w.log.verticalScrollBar().value() == \
                        w.log.verticalScrollBar().maximum()
                    if w.tabs.currentIndex() == 1 and not w.log.toPlainText().startswith("restorectl wtf"):
                        w.log.setPlainText(fh.read().decode("utf-8", "replace"))
                        if at_end:
                            w.log.verticalScrollBar().setValue(
                                w.log.verticalScrollBar().maximum())
            except OSError:
                pass

    def show_window(self):
        self.poll()
        self.load_snapshots()
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()


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
            fh.write(AUTOSTART.format(
                exe=shutil.which("restorectl") or "/usr/local/bin/restorectl"))
        print(f"autostart installed: {path}")
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit("no system tray available")
    Tray(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
