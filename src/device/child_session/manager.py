"""Windows Child Session (桌面分身) 管理器。

调用 tools/desktop_avatar/DesktopAvatar.exe 实现独立的 Windows 子会话运行环境，
使游戏与自动化脚本在完全隔离的虚拟桌面中运行，实现免切屏、真后台、不抢占物理鼠标。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from typing import Optional

from src.config.paths import get_app_root

logger = logging.getLogger(__name__)

_GLOBAL_MANAGER: Optional[ChildSessionManager] = None


class ChildSessionManager:
    """管理桌面分身伴侣程序及其生命周期。"""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None

    @classmethod
    def get_instance(cls) -> ChildSessionManager:
        global _GLOBAL_MANAGER
        if _GLOBAL_MANAGER is None:
            _GLOBAL_MANAGER = ChildSessionManager()
        return _GLOBAL_MANAGER

    def get_avatar_exe_path(self) -> str:
        """获取 DesktopAvatar.exe 的完整路径。"""
        root = get_app_root()
        candidate = os.path.join(root, "tools", "desktop_avatar", "DesktopAvatar.exe")
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        # 兜底：支持与 exe 同级放置的情况
        alt = os.path.join(root, "DesktopAvatar.exe")
        if os.path.isfile(alt):
            return os.path.abspath(alt)
        return candidate

    def is_supported(self) -> bool:
        """检查当前操作系统是否支持桌面分身特性。"""
        if sys.platform != "win32":
            return False
        # Windows 10 (Build 10.0.17763+) 或 Windows 11
        try:
            ver = sys.getwindowsversion()
            return ver.major >= 10
        except Exception:
            return False

    def is_available(self) -> bool:
        """检查伴侣程序二进制文件是否存在。"""
        return os.path.isfile(self.get_avatar_exe_path())

    def is_running(self) -> bool:
        """检查当前伴侣程序是否处于运行状态。"""
        if self._process is not None:
            if self._process.poll() is None:
                return True
            self._process = None

        # 检查操作系统进程中是否存在 DesktopAvatar
        try:
            output = subprocess.check_output(
                ["tasklist", "/fi", "imagename eq DesktopAvatar.exe", "/fo", "csv", "/nh"],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                text=True,
                errors="ignore",
            )
            return "DesktopAvatar.exe" in output
        except Exception:
            return False

    def get_active_child_session_id(self) -> Optional[int]:
        """安全读取当前系统已分配的 Child Session ID（若未建立则返回 None）。"""
        if not self.is_supported():
            return None
        try:
            sid = ctypes.c_uint32()
            res = ctypes.windll.wtsapi32.WTSGetChildSessionId(ctypes.byref(sid))
            # 0xFFFFFFFF 表示无激活的子会话
            if res and sid.value != 0xFFFFFFFF:
                return int(sid.value)
        except Exception as exc:
            logger.debug("读取 ChildSessionId 失败: %s", exc)
        return None

    def launch_avatar(
        self,
        launch_target: Optional[str] = None,
        width: int = 1920,
        height: int = 1080,
        title: Optional[str] = None,
    ) -> bool:
        """启动桌面分身窗口。

        :param launch_target: 分身就绪后自动拉起的目标程序路径 (可选)
        :param width: 分身虚拟桌面的宽度 (默认 1920)
        :param height: 分身虚拟桌面的高度 (默认 1080)
        :param title: 分身窗口标题 (可选)
        :return: 是否成功启动进程
        """
        exe_path = self.get_avatar_exe_path()
        if not os.path.isfile(exe_path):
            logger.error("未找到 DesktopAvatar.exe: %s", exe_path)
            return False

        if self.is_running():
            logger.info("DesktopAvatar 已经在运行中")
            return True

        cmd = [exe_path, "--width", str(width), "--height", str(height)]
        if launch_target:
            cmd.extend(["--launch", launch_target])
        if title:
            cmd.extend(["--title", title])

        try:
            logger.info("启动桌面分身程序: %s", " ".join(cmd))
            self._process = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(exe_path),
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
            return True
        except Exception as exc:
            logger.error("启动桌面分身失败: %s", exc)
            return False

    def terminate_avatar(self, logoff_session: bool = True) -> bool:
        """终止桌面分身进程并注销子会话。"""
        exe_path = self.get_avatar_exe_path()
        if logoff_session and os.path.isfile(exe_path):
            try:
                subprocess.run(
                    [exe_path, "--logoff-only"],
                    creationflags=0x08000000,
                    timeout=5,
                )
            except Exception as exc:
                logger.debug("注销子会话失败: %s", exc)

        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None

        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "DesktopAvatar.exe"],
                creationflags=0x08000000,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


def get_child_session_manager() -> ChildSessionManager:
    return ChildSessionManager.get_instance()
