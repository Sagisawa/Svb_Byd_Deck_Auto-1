from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.bridge.detector import DEFAULT_CANDIDATE_LOG_PATHS, resolve_active_log_path

logger = logging.getLogger(__name__)

CANDIDATE_SESSION_LOG_PATHS = DEFAULT_CANDIDATE_LOG_PATHS


class TrackerBridge:
    """Bridges game state from SephiesDeckLab's app_session.jsonl into Svb_Byd_Deck_Auto.

    Runs an efficient tail-reader thread on the session log, providing thread-safe
    access to the most recent snapshot. Supports automatic fallback across candidate paths
    and dynamic detection of running Lab processes.
    """

    def __init__(self, log_path: Optional[str] = None, *, poll_interval: float = 0.1) -> None:
        self.explicit_log_path = log_path
        self.log_path = self._resolve_log_path()
        self.poll_interval = poll_interval

        self._lock = threading.RLock()
        self._latest_snapshot: Optional[Dict[str, Any]] = None
        self._latest_timestamp: float = 0.0
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def _resolve_log_path(self) -> str:
        return resolve_active_log_path(self.explicit_log_path)

    def set_log_path(self, log_path: Optional[str]) -> None:
        """Dynamically update the tracked log file path."""
        with self._lock:
            self.explicit_log_path = log_path
            resolved = self._resolve_log_path()
            if self.log_path != resolved:
                self.log_path = resolved
                logger.info("TrackerBridge log path updated to: %s", self.log_path)
                # 清除旧路径下的陈旧快照，防止切换到错误窗口或不存在的文件时残留上一局状态
                self._latest_snapshot = None
                self._latest_timestamp = 0.0
                if os.path.isfile(resolved):
                    self._load_latest_on_start()
            elif not os.path.isfile(resolved):
                # 即使路径相同但文件不存在，也清空快照保证 is_fresh / is_available 返回 False
                self._latest_snapshot = None
                self._latest_timestamp = 0.0

    def start(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        # 启动前立即尝试加载已有的最新快照，确保首次调用立即可用
        self._load_latest_on_start()
        self._worker_thread = threading.Thread(
            target=self._tail_loop,
            name="SephieBridgeTailReader",
            daemon=True,
        )
        self._worker_thread.start()
        logger.info("TrackerBridge started tailing %s", self.log_path)

    def _load_latest_on_start(self) -> None:
        try:
            target = self.log_path or self._resolve_log_path()
            if target and os.path.isfile(target) and os.path.getsize(target) > 0:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    if lines:
                        for l in reversed(lines):
                            if l.strip() and self._parse_and_update(l.strip()):
                                break
            else:
                with self._lock:
                    if self.explicit_log_path not in (None, "", "auto"):
                        self._latest_snapshot = None
                        self._latest_timestamp = 0.0
        except Exception as exc:
            logger.debug("Immediate snapshot load failed: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)
            self._worker_thread = None
        logger.info("TrackerBridge stopped")

    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def refresh_from_file(self) -> Optional[Dict[str, Any]]:
        """直接从日志文件末尾读取最新快照，不依赖后台轮询延迟。"""
        try:
            target = self.log_path or self._resolve_log_path()
            if target and os.path.isfile(target) and os.path.getsize(target) > 0:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    size = os.path.getsize(target)
                    seek_pos = max(0, size - 65536)
                    f.seek(seek_pos)
                    lines = f.readlines()
                    if lines:
                        for l in reversed(lines):
                            line = l.strip()
                            if line and self._parse_and_update(line):
                                break
            else:
                with self._lock:
                    if self.explicit_log_path not in (None, "", "auto"):
                        self._latest_snapshot = None
                        self._latest_timestamp = 0.0
        except Exception as exc:
            logger.debug("Immediate snapshot refresh failed: %s", exc)
        with self._lock:
            return self._latest_snapshot

    def get_snapshot(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        if force_refresh:
            self.refresh_from_file()
        with self._lock:
            return self._latest_snapshot

    def get_snapshot_age(self) -> float:
        with self._lock:
            if self._latest_timestamp == 0.0:
                return float("inf")
            return time.time() - self._latest_timestamp

    def is_fresh(self, max_age: float = 30.0) -> bool:
        return self.get_snapshot_age() <= max_age

    def clear_snapshot(self) -> None:
        """显式清空当前缓存的快照，用于对战结束或对局重置时防止旧数据残留。"""
        with self._lock:
            self._latest_snapshot = None
            self._latest_timestamp = 0.0

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _tail_loop(self) -> None:
        last_file_size = 0
        file_obj = None
        current_tracked_path = ""
        last_auto_check = 0.0

        while not self._stop_event.is_set():
            try:
                now = time.time()
                # In auto mode, periodically check if running Lab process appeared or changed
                if self.explicit_log_path in (None, "", "auto") and (now - last_auto_check > 2.5):
                    last_auto_check = now
                    active = self._resolve_log_path()
                    if active and active != self.log_path and os.path.isfile(active):
                        logger.info("TrackerBridge switched to running Lab log: %s -> %s", self.log_path, active)
                        self.log_path = active

                target_path = self.log_path or self._resolve_log_path()
                # If path changed or target is not a valid existing file, handle cleanly
                if not (target_path and os.path.isfile(target_path)):
                    if file_obj is not None:
                        try:
                            file_obj.close()
                        except Exception:
                            pass
                        file_obj = None
                        current_tracked_path = ""
                        last_file_size = 0
                    if self.explicit_log_path in (None, "", "auto"):
                        resolved = self._resolve_log_path()
                        if resolved and os.path.isfile(resolved):
                            target_path = resolved
                            self.log_path = resolved
                        else:
                            with self._lock:
                                self._latest_snapshot = None
                                self._latest_timestamp = 0.0
                            time.sleep(self.poll_interval)
                            continue
                    else:
                        with self._lock:
                            self._latest_snapshot = None
                            self._latest_timestamp = 0.0
                        time.sleep(self.poll_interval)
                        continue

                curr_size = os.path.getsize(target_path)

                # Detect file change, rotation or initial open
                if file_obj is None or current_tracked_path != target_path or curr_size < last_file_size:
                    if file_obj is not None:
                        try:
                            file_obj.close()
                        except Exception:
                            pass
                    file_obj = open(target_path, "r", encoding="utf-8", errors="replace")
                    current_tracked_path = target_path
                    # Read last line on initial attach
                    if curr_size > 0:
                        lines = file_obj.readlines()
                        if lines:
                            for l in reversed(lines):
                                if l.strip():
                                    self._parse_and_update(l.strip())
                                    break
                        last_file_size = file_obj.tell()
                    else:
                        last_file_size = 0

                if curr_size > last_file_size:
                    file_obj.seek(last_file_size)
                    new_lines = file_obj.readlines()
                    last_file_size = file_obj.tell()
                    if new_lines:
                        for line in reversed(new_lines):
                            line = line.strip()
                            if line:
                                if self._parse_and_update(line):
                                    break

                time.sleep(self.poll_interval)
            except Exception as exc:
                logger.debug("TrackerBridge tail error: %s", exc)
                time.sleep(self.poll_interval * 2)

        if file_obj is not None:
            try:
                file_obj.close()
            except Exception:
                pass

    def _parse_and_update(self, line: str) -> bool:
        try:
            data = json.loads(line)
            snapshot = data.get("snapshot") if isinstance(data, dict) else None
            if not isinstance(snapshot, dict):
                return False

            now = time.time()
            record_time = now
            if isinstance(data, dict) and "timestamp" in data:
                try:
                    dt = datetime.fromisoformat(str(data["timestamp"]))
                    parsed_ts = dt.timestamp()
                    if 0 < parsed_ts <= now + 5.0:
                        record_time = parsed_ts
                except Exception:
                    record_time = now

            with self._lock:
                self._latest_snapshot = snapshot
                self._latest_timestamp = record_time
                subscribers = list(self._subscribers)

            for sub in subscribers:
                try:
                    sub(snapshot)
                except Exception as exc:
                    logger.warning("Subscriber error in TrackerBridge: %s", exc)
            return True
        except json.JSONDecodeError:
            return False


# Global singleton instance
_GLOBAL_TRACKER_BRIDGE: Optional[TrackerBridge] = None
_BRIDGE_LOCK = threading.Lock()


def get_global_tracker_bridge(log_path: Optional[str] = None) -> TrackerBridge:
    global _GLOBAL_TRACKER_BRIDGE
    with _BRIDGE_LOCK:
        if _GLOBAL_TRACKER_BRIDGE is None:
            _GLOBAL_TRACKER_BRIDGE = TrackerBridge(log_path)
        elif log_path is not None and _GLOBAL_TRACKER_BRIDGE.explicit_log_path != log_path:
            _GLOBAL_TRACKER_BRIDGE.set_log_path(log_path)
        return _GLOBAL_TRACKER_BRIDGE
