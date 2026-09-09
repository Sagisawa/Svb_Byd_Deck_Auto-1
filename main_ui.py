#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形界面入口。

该文件仅保留启动所需的最小编排逻辑。
"""

from __future__ import annotations

import os
import sys

# 设置环境变量以避免PyTorch的pin_memory警告
os.environ["PIN_MEMORY"] = "false"

# 添加项目目录到Python路径（兼容从任意工作目录启动）
_project_root = os.path.dirname(__file__)

if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.app.bootstrap import run_gui


def check_and_elevate():
    """若非管理员，自动请求提升为管理员权限以穿透 Windows UIPI 控制端游。"""
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            script = os.path.abspath(sys.argv[0])
            params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                sys.executable,
                f'"{script}" {params}'.strip(),
                None,
                1,
            )
            if int(ret) > 32:
                sys.exit(0)
    except Exception:
        pass


def main():
    check_and_elevate()
    sys.exit(run_gui(sys.argv))

if __name__ == "__main__":
    main()
