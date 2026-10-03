"""用于动态提取游戏版本特征码的后台 QThread 工作线程。"""

from __future__ import annotations

from typing import Any, Dict, Optional
from PyQt5.QtCore import QObject, QThread, pyqtSignal

from src.tracker.versioning import extract_and_save_profile_for_process


class ProfileExtractionWorker(QThread):
    finished_signal = pyqtSignal(bool, str, dict)

    def __init__(self, pid: Optional[int] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pid = pid

    def run(self):
        try:
            success, msg, data = extract_and_save_profile_for_process(self.pid)
            self.finished_signal.emit(success, msg, data or {})
        except Exception as e:
            self.finished_signal.emit(False, str(e), {})
