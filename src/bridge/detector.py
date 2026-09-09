"""Sephie's Lab / ShadowverseTracker process detector and log path resolver.

Uses Windows Toolhelp32Snapshot and WMI to discover running instances of
Sephie's Lab / ShadowverseTracker and locates their app_session.jsonl log files.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import glob
import logging
import os
from pathlib import Path
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Executable names of known Lab / Tracker tools (case-insensitive)
KNOWN_LAB_EXE_NAMES = {
    "sephie's lab.exe",
    "sephies_lab.exe",
    "sephieslab.exe",
    "shadowversetracker.exe",
    "shadowverse_tracker.exe",
}

DEFAULT_CANDIDATE_LOG_PATHS = [
    r"D:\auto\DaydreamStarRiver\dist\Sephie's Lab\logs\app_session.jsonl",
    r"D:\auto\DaydreamStarRiver\logs\app_session.jsonl",
    r"D:\auto\SephiesDeckLab-1.0.0\dist\Sephie's Lab\logs\app_session.jsonl",
    r"D:\auto\SephiesDeckLab-1.0.0\logs\app_session.jsonl",
]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


@dataclass
class LabTargetInfo:
    label: str
    log_path: str
    pid: Optional[int] = None
    exe_path: Optional[str] = None
    is_running: bool = False
    log_exists: bool = False
    log_size: int = 0


def _get_process_image_path(pid: int) -> Optional[str]:
    """Retrieve full executable path for a process ID using Windows API."""
    kernel32 = ctypes.windll.kernel32
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h_proc)
    return None


def _resolve_log_path_for_exe(exe_path: str) -> str:
    """Infer app_session.jsonl path given executable path."""
    if not exe_path or not os.path.isabs(exe_path):
        return ""
    exe_dir = os.path.dirname(os.path.abspath(exe_path))
    # 1. <exe_dir>/logs/app_session.jsonl
    p1 = os.path.join(exe_dir, "logs", "app_session.jsonl")
    if os.path.exists(p1):
        return p1
    # 2. <exe_dir>/../logs/app_session.jsonl (if running from dist/...)
    p2 = os.path.abspath(os.path.join(exe_dir, "..", "logs", "app_session.jsonl"))
    if os.path.exists(p2):
        return p2
    # 3. Default to <exe_dir>/logs/app_session.jsonl
    return p1


def find_running_lab_processes() -> List[LabTargetInfo]:
    """Find currently running Sephie's Lab / ShadowverseTracker processes."""
    running_targets: List[LabTargetInfo] = []
    seen_pids: Set[int] = set()

    # Pass 1: Toolhelp32Snapshot for compiled executables
    kernel32 = ctypes.windll.kernel32
    h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snap and h_snap != -1:
        try:
            pe32 = PROCESSENTRY32W()
            pe32.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if kernel32.Process32FirstW(h_snap, ctypes.byref(pe32)):
                while True:
                    exe_name = pe32.szExeFile
                    pid = pe32.th32ProcessID
                    if exe_name.lower() in KNOWN_LAB_EXE_NAMES and pid not in seen_pids:
                        seen_pids.add(pid)
                        full_exe = _get_process_image_path(pid) or exe_name
                        log_path = _resolve_log_path_for_exe(full_exe) if os.path.isabs(full_exe) else ""
                        log_exists = bool(log_path and os.path.exists(log_path))
                        log_size = os.path.getsize(log_path) if log_exists else 0

                        dir_display = os.path.dirname(full_exe) if os.path.isabs(full_exe) else ""
                        label = f"[运行中] {exe_name} (PID: {pid}) - {dir_display}"
                        running_targets.append(
                            LabTargetInfo(
                                label=label,
                                log_path=log_path,
                                pid=pid,
                                exe_path=full_exe,
                                is_running=True,
                                log_exists=log_exists,
                                log_size=log_size,
                            )
                        )
                    if not kernel32.Process32NextW(h_snap, ctypes.byref(pe32)):
                        break
        finally:
            kernel32.CloseHandle(h_snap)

    # Pass 2: Check Python processes running qt_app.py or app.py via WMI if available
    try:
        import win32com.client
        wmi = win32com.client.GetObject("winmgmts:")
        for p in wmi.InstancesOf("Win32_Process"):
            pid = int(p.ProcessId)
            if pid in seen_pids:
                continue
            name = str(p.Name or "").lower()
            cmd = str(p.CommandLine or "")
            if name in ("python.exe", "pythonw.exe") and any(
                k in cmd.lower() for k in ("qt_app.py", "app.py", "sephie", "shadowversetracker")
            ):
                seen_pids.add(pid)
                # Try to extract working directory or script path
                full_exe = _get_process_image_path(pid) or name
                script_dir = ""
                matched_script = ""
                for part in cmd.split():
                    clean_part = part.strip("\"'")
                    if clean_part.endswith(".py") and os.path.exists(clean_part):
                        script_name = os.path.basename(clean_part).lower()
                        if script_name in ("qt_app.py", "app.py", "tracker_service.py", "main.py"):
                            script_dir = os.path.dirname(os.path.abspath(clean_part))
                            matched_script = script_name
                            break

                if not script_dir:
                    continue

                log_path = os.path.join(script_dir, "logs", "app_session.jsonl")
                log_exists = bool(os.path.exists(log_path))
                log_size = os.path.getsize(log_path) if log_exists else 0

                label = f"[运行中 Python] PID: {pid} ({matched_script}) - {script_dir}"
                running_targets.append(
                    LabTargetInfo(
                        label=label,
                        log_path=log_path,
                        pid=pid,
                        exe_path=full_exe,
                        is_running=True,
                        log_exists=log_exists,
                        log_size=log_size,
                    )
                )
    except Exception as exc:
        logger.debug("WMI python process inspection skipped: %s", exc)

    return running_targets


def get_process_id_and_path_from_hwnd(hwnd: int) -> Tuple[int, str]:
    """Retrieve process ID and full executable path from a window handle."""
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_val = int(pid.value)
    exe_path = _get_process_image_path(pid_val) or ""
    return pid_val, exe_path


def get_all_candidate_lab_targets(
    preferred_path: Optional[str] = None,
    include_all_windows: bool = True,
) -> List[LabTargetInfo]:
    """Get all candidate targets for UI dropdown:
    1. 【自动探测】(Auto-detect) option as top recommendation.
    2. Detected Lab / Tracker processes (★ [推荐]).
    3. ALL other running application windows (allowing manual lock if renamed).
    4. Local historical log files.
    5. 【自定义】Browse file option.
    """
    targets: List[LabTargetInfo] = []
    seen_pids: Set[int] = set()
    known_log_paths: Set[str] = set()

    running = find_running_lab_processes()

    # 1. Auto-detect option
    if running:
        first = running[0]
        exe_base = os.path.basename(first.exe_path or "Sephie's Lab.exe")
        auto_label = f"【自动探测】推荐匹配运行中的Lab/Tracker工具 (已锁定: PID {first.pid} - {exe_base})"
    else:
        auto_label = "【自动探测】推荐匹配运行中的Lab/Tracker工具"
    targets.append(
        LabTargetInfo(
            label=auto_label,
            log_path="auto",
            is_running=bool(running),
        )
    )

    # 2. Add detected running Lab tools as recommended
    for r in running:
        if r.pid:
            seen_pids.add(r.pid)
        exe_base = os.path.basename(r.exe_path or "Sephie's Lab.exe")
        label = f"★ [推荐] {exe_base}  (PID: {r.pid})"
        targets.append(
            LabTargetInfo(
                label=label,
                log_path=r.log_path,
                pid=r.pid,
                exe_path=r.exe_path,
                is_running=True,
                log_exists=r.log_exists,
                log_size=r.log_size,
            )
        )
        if r.log_path:
            known_log_paths.add(os.path.normcase(os.path.abspath(r.log_path)))

    # 3. Add all running desktop application windows (just like "游戏窗口")
    if include_all_windows:
        try:
            from src.device.wgc import list_candidate_windows

            windows = list_candidate_windows()
        except Exception:
            windows = []

        my_pid = os.getpid()
        other_window_targets: List[LabTargetInfo] = []
        for hwnd, raw_label, title, is_game in windows:
            pid, exe_path = get_process_id_and_path_from_hwnd(hwnd)
            if not pid or pid == my_pid or pid in seen_pids:
                continue
            seen_pids.add(pid)

            # Clean display title
            clean_title = title.split(" (")[0].strip() if " (" in title else title
            if not clean_title or "Shadowverse Auto Control Center" in clean_title:
                continue

            exe_base = os.path.basename(exe_path) if exe_path else ""
            log_path = _resolve_log_path_for_exe(exe_path) if exe_path else ""
            log_exists = bool(log_path and os.path.exists(log_path))
            log_sz = os.path.getsize(log_path) if log_exists else 0

            is_likely_lab = (
                exe_base.lower() in KNOWN_LAB_EXE_NAMES
                or any(k in title.lower() for k in ("sephie", "tracker", "decklab", "deck lab"))
            )

            if is_likely_lab:
                label = f"★ [推荐] {clean_title}  (PID: {pid})"
                targets.append(
                    LabTargetInfo(
                        label=label,
                        log_path=log_path,
                        pid=pid,
                        exe_path=exe_path,
                        is_running=True,
                        log_exists=log_exists,
                        log_size=log_sz,
                    )
                )
                if log_path:
                    known_log_paths.add(os.path.normcase(os.path.abspath(log_path)))
            else:
                log_tag = "" if log_exists else " [未检测到日志]"
                label = f"{clean_title}  (PID: {pid}){log_tag}"
                other_window_targets.append(
                    LabTargetInfo(
                        label=label,
                        log_path=log_path,
                        pid=pid,
                        exe_path=exe_path,
                        is_running=True,
                        log_exists=log_exists,
                        log_size=log_sz,
                    )
                )

        # Append other window targets after recommended ones
        targets.extend(other_window_targets)

    # 4. Discover existing static candidate log files
    static_candidates: List[str] = list(DEFAULT_CANDIDATE_LOG_PATHS)
    auto_root = r"D:\auto"
    if os.path.isdir(auto_root):
        patterns = [
            os.path.join(auto_root, "*", "dist", "Sephie's Lab", "logs", "app_session.jsonl"),
            os.path.join(auto_root, "*", "logs", "app_session.jsonl"),
        ]
        for pat in patterns:
            try:
                for match in glob.glob(pat):
                    static_candidates.append(match)
            except Exception:
                pass

    for raw_path in static_candidates:
        abs_p = os.path.abspath(raw_path)
        norm = os.path.normcase(abs_p)
        if norm in known_log_paths:
            continue
        known_log_paths.add(norm)

        if os.path.exists(abs_p):
            sz = os.path.getsize(abs_p)
            parent_dir = os.path.basename(os.path.dirname(os.path.dirname(abs_p)))
            label = f"【本地日志】{parent_dir} ({abs_p})"
            targets.append(
                LabTargetInfo(
                    label=label,
                    log_path=abs_p,
                    is_running=False,
                    log_exists=True,
                    log_size=sz,
                )
            )

    # 5. If preferred_path was provided and not yet present
    if preferred_path and preferred_path not in ("auto", "__browse__"):
        norm_pref = os.path.normcase(os.path.abspath(preferred_path))
        if norm_pref not in known_log_paths:
            exists = os.path.exists(preferred_path)
            sz = os.path.getsize(preferred_path) if exists else 0
            targets.insert(
                1,
                LabTargetInfo(
                    label=f"【当前保存】{preferred_path}",
                    log_path=preferred_path,
                    is_running=False,
                    log_exists=exists,
                    log_size=sz,
                ),
            )

    # 6. Manual file browse option at the end
    targets.append(
        LabTargetInfo(
            label="【自定义】浏览本地日志文件 (app_session.jsonl)...",
            log_path="__browse__",
            is_running=False,
        )
    )

    return targets


def resolve_active_log_path(explicit_path: Optional[str] = None) -> str:
    """Resolve the effective log path to tail.

    If explicit_path is provided and is a concrete file path, returns it.
    If explicit_path is None or 'auto', prioritizes running processes, then existing files.
    """
    if explicit_path and explicit_path != "auto":
        return explicit_path

    # Check running processes first
    running = find_running_lab_processes()
    for proc in running:
        if proc.log_path:
            return proc.log_path

    # Fallback to existing candidate log files
    for path in DEFAULT_CANDIDATE_LOG_PATHS:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    for path in DEFAULT_CANDIDATE_LOG_PATHS:
        if os.path.exists(path):
            return path

    return DEFAULT_CANDIDATE_LOG_PATHS[0]
