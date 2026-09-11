"""Windows Child Session (桌面分身) 管理器。

调用 tools/desktop_avatar/DesktopAvatar.exe 实现独立的 Windows 子会话运行环境，
使游戏与自动化脚本在完全隔离的虚拟桌面中运行，实现免切屏、真后台、不抢占物理鼠标。
"""

from __future__ import annotations

import base64
import ctypes
import getpass
import logging
import os
import subprocess
import sys
from typing import Optional, Tuple

from src.config.paths import get_app_root, is_frozen

logger = logging.getLogger(__name__)

_GLOBAL_MANAGER: Optional[ChildSessionManager] = None


def encode_credential_secret(plain: str) -> str:
    """简单对本地凭据密码做混淆编码保存。"""
    if not plain:
        return ""
    try:
        raw = plain.encode("utf-8")
        return "b64:" + base64.b64encode(raw).decode("ascii")
    except Exception:
        return plain


def decode_credential_secret(secret: str) -> str:
    """解析已编码的凭据密码。"""
    if not secret:
        return ""
    if secret.startswith("b64:"):
        try:
            raw = base64.b64decode(secret[4:].encode("ascii"))
            return raw.decode("utf-8")
        except Exception:
            return ""
    return secret


def find_default_script_path() -> Tuple[str, str, str]:
    """探测 Svb_Byd_Deck_Auto 的启动可执行文件、启动参数和工作目录。"""
    root = get_app_root()
    if is_frozen():
        return sys.executable, "", os.path.dirname(sys.executable)
    dist_exe = os.path.join(root, "dist", "Svb_Byd_Deck_Auto", "Svb_Byd_Deck_Auto.exe")
    if os.path.isfile(dist_exe):
        return os.path.abspath(dist_exe), "", os.path.dirname(dist_exe)
    root_exe = os.path.join(root, "Svb_Byd_Deck_Auto.exe")
    if os.path.isfile(root_exe):
        return os.path.abspath(root_exe), "", root
    main_ui = os.path.join(root, "main_ui.py")
    if os.path.isfile(main_ui):
        py_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(py_dir, "pythonw.exe")
        py_exec = pythonw if os.path.isfile(pythonw) else sys.executable
        return os.path.abspath(py_exec), f'"{os.path.abspath(main_ui)}"', root
    return "", "", root


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

    def get_child_session_config(self) -> dict:
        """从全局配置读取桌面分身设置。"""
        from src.config.config_repository import ConfigRepository
        from src.config.paths import get_config_path

        current_user = ""
        try:
            current_user = getpass.getuser()
        except Exception:
            current_user = "32698"

        default_script, default_args, default_workdir = find_default_script_path()

        default_cfg = {
            "enabled": True,
            "resolution": "1920x1080",
            "auto_launch_enabled": True,
            "launch_delay_seconds": 2,
            "auto_launch_programs": [],
            "auto_login": {
                "enabled": True,
                "username": current_user,
                "password": "",
            },
            "script_program": {
                "path": default_script,
                "args": default_args,
                "working_dir": default_workdir,
            },
        }
        try:
            repo = ConfigRepository(get_config_path())
            cfg, _, _ = repo.load_existing(allow_default_on_error=True)
            if isinstance(cfg, dict):
                cs = cfg.get("child_session")
                if isinstance(cs, dict):
                    merged = dict(default_cfg)
                    merged.update(cs)
                    # 递归合并嵌套字典
                    if isinstance(cs.get("auto_login"), dict):
                        merged["auto_login"] = dict(default_cfg["auto_login"])
                        merged["auto_login"].update(cs["auto_login"])
                    if isinstance(cs.get("script_program"), dict):
                        merged["script_program"] = dict(default_cfg["script_program"])
                        merged["script_program"].update(cs["script_program"])
                    return merged
                # 兼容旧配置中的分辨率键
                old_res = cfg.get("child_session_resolution")
                if old_res:
                    default_cfg["resolution"] = str(old_res)
        except Exception as exc:
            logger.debug("读取 child_session 配置失败: %s", exc)
        return default_cfg

    def save_child_session_config(self, cs_config: dict) -> bool:
        """将桌面分身设置保存至全局配置。"""
        from src.config.config_repository import ConfigRepository
        from src.config.paths import get_config_path

        try:
            repo = ConfigRepository(get_config_path())
            res = repo.update({
                "child_session": cs_config,
                "child_session_resolution": cs_config.get("resolution", "1920x1080"),
            })
            return res.ok
        except Exception as exc:
            logger.error("保存 child_session 配置失败: %s", exc)
            return False

    def launch_program_in_child_session(
        self,
        executable_path: str,
        arguments: str = "",
        working_dir: str = "",
    ) -> bool:
        """向当前运行的桌面分身 (Child Session) 中即时注入启动程序。
        
        使用与顶栏「运行程序」完全一致的 Windows 任务计划程序 COM 接口，
        赋予最高管理员权限并绕过 UAC 拦截。
        """
        exe_path = self.get_avatar_exe_path()
        if not os.path.isfile(exe_path):
            logger.error("未找到 DesktopAvatar.exe: %s", exe_path)
            return False

        if not os.path.isfile(executable_path):
            logger.error("目标启动程序不存在: %s", executable_path)
            return False

        cmd = [exe_path, "--launch-only", executable_path]
        if arguments:
            cmd.extend(["--args", arguments])
        if working_dir:
            cmd.extend(["--workdir", working_dir])

        try:
            proc = subprocess.run(
                cmd,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                timeout=10,
            )
            return proc.returncode == 0
        except Exception as exc:
            logger.error("注入启动程序失败: %s", exc)
            return False

    def launch_avatar(
        self,
        launch_target: Optional[str] = None,
        launch_config_file: Optional[str] = None,
        width: int = 1920,
        height: int = 1080,
        title: Optional[str] = None,
    ) -> bool:
        """启动桌面分身窗口。

        :param launch_target: 分身就绪后自动拉起的目标程序路径 (可选)
        :param launch_config_file: 包含自启程序列表的配置文件路径 (可选)
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

        # 如果未显式提供 launch_config_file，自动检查配置并输出综合启动清单
        if not launch_config_file and not launch_target:
            try:
                import json
                cfg = self.get_child_session_config()

                user_name = ""
                user_pwd = ""
                auto_login = cfg.get("auto_login", {})
                if isinstance(auto_login, dict) and auto_login.get("enabled", True):
                    user_name = str(auto_login.get("username", "") or "")
                    user_pwd = decode_credential_secret(str(auto_login.get("password", "") or ""))

                script_cfg = cfg.get("script_program", {})
                script_path = ""
                script_args = ""
                script_workdir = ""
                if isinstance(script_cfg, dict):
                    script_path = str(script_cfg.get("path", "") or "")
                    script_args = str(script_cfg.get("args", "") or "")
                    script_workdir = str(script_cfg.get("working_dir", "") or "")
                if not script_path:
                    script_path, script_args, script_workdir = find_default_script_path()

                items = []
                if cfg.get("auto_launch_enabled", True):
                    progs = cfg.get("auto_launch_programs", [])
                    enabled_progs = [p for p in progs if isinstance(p, dict) and p.get("enabled", True)]
                    init_delay = int(cfg.get("launch_delay_seconds", 2))
                    for idx, p in enumerate(enabled_progs):
                        item_delay = int(p.get("delay_seconds", 0))
                        if idx == 0 and init_delay > 0 and item_delay == 0:
                            item_delay = init_delay
                        items.append({
                            "Name": str(p.get("name") or os.path.basename(p.get("path", ""))),
                            "Path": str(p.get("path", "")),
                            "Arguments": str(p.get("args", "")),
                            "WorkingDirectory": str(p.get("working_dir", "")),
                            "DelaySeconds": item_delay,
                            "Enabled": True,
                        })

                payload = {
                    "UserName": user_name,
                    "Password": user_pwd,
                    "ScriptPath": script_path,
                    "ScriptArguments": script_args,
                    "ScriptWorkingDirectory": script_workdir,
                    "AutoLaunchItems": items,
                }
                temp_dir = os.path.dirname(exe_path)
                cfg_file = os.path.join(temp_dir, "auto_launch_cache.json")
                with open(cfg_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                launch_config_file = cfg_file
            except Exception as exc:
                logger.warning("生成启动配置文件失败: %s", exc)

        cmd = [exe_path, "--width", str(width), "--height", str(height)]
        if launch_target:
            cmd.extend(["--launch", launch_target])
        if launch_config_file and os.path.isfile(launch_config_file):
            cmd.extend(["--launch-config", launch_config_file])
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
