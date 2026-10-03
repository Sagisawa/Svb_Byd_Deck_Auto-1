"""Shadowverse: Worlds Beyond 游戏版本特征码自动提取与配置生成工具.

当游戏更新导致内存直读失效时，在游戏运行状态下直接执行本脚本：
    python tools/auto_update_profile.py

脚本会自动：
1. 捕获正在运行的 Shadowverse 游戏进程；
2. 提取 EXE 版本号 (如 1.9.11.19463) 与 GameAssembly.dll SHA256；
3. 动态扫描 IL2CPP 核心类 (BattleModel, DeckInfo, PracticeBattleModel) 的全局指针 RVA；
4. 自动生成并保存到 src/tracker/version_profiles/<version>.json；
5. 完成后立即生效，无需手动逆向分析。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

# 确保能导入 src 模块
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tracker.memory.win32 import ProcessReader, iter_processes
from src.tracker.memory.discovery import find_il2cpp_classes


TARGET_CLASSES = (
    ("battle_model_class_pointer_rva", "BattleModel", "Wizard2.Presentation.Battle"),
    ("deck_info_class_pointer_rva", "DeckInfo", "Wizard2.Domain.DeckInfoData"),
    ("practice_battle_model_class_pointer_rva", "PracticeBattleModel", "Wizard2.Presentation.Practice"),
)


def get_file_version(file_path: str | Path) -> str:
    """读取 Windows 可执行文件的文件版本号 (如 1.9.11.19463)."""
    file_path = str(file_path)
    size = ctypes.windll.version.GetFileVersionInfoSizeW(file_path, None)
    if not size:
        return ""
    res = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(file_path, 0, size, res):
        return ""
    u_len = wintypes.UINT()
    l_ptr = ctypes.c_void_p()
    # \VarFileInfo\Translation
    if not ctypes.windll.version.VerQueryValueW(
        res, r"\VarFileInfo\Translation", ctypes.byref(l_ptr), ctypes.byref(u_len)
    ):
        return ""
    buf = ctypes.cast(l_ptr, ctypes.POINTER(ctypes.c_uint16))
    sub_block = f"\\StringFileInfo\\{buf[0]:04x}{buf[1]:04x}\\FileVersion"
    if ctypes.windll.version.VerQueryValueW(
        res, sub_block, ctypes.byref(l_ptr), ctypes.byref(u_len)
    ):
        return ctypes.wstring_at(l_ptr.value)
    # 尝试 ProductVersion
    sub_block = f"\\StringFileInfo\\{buf[0]:04x}{buf[1]:04x}\\ProductVersion"
    if ctypes.windll.version.VerQueryValueW(
        res, sub_block, ctypes.byref(l_ptr), ctypes.byref(u_len)
    ):
        return ctypes.wstring_at(l_ptr.value)
    return ""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def extract_profile_from_process(pid: int, save: bool = True) -> dict[str, str] | None:
    print(f"[*] 正在附加到游戏进程 (PID: {pid})...")
    reader = ProcessReader(pid)
    try:
        module = reader.module("GameAssembly.dll")
        exe_path = reader.module("ShadowverseWB.exe").path
        unity_path = None
        try:
            unity_path = reader.module("UnityPlayer.dll").path
        except Exception:
            pass

        print(f"[+] 找到 GameAssembly.dll: 基址 {hex(module.base_address)}, 大小 {hex(module.size)}")
        print(f"[*] 计算 GameAssembly.dll SHA256 哈希...")
        sha256 = sha256_file(module.path)
        print(f"[+] SHA256: {sha256}")

        # 获取版本号
        game_version = get_file_version(exe_path) or "1.0.0"
        unity_version = (get_file_version(unity_path) if unity_path else "") or "2022.3.62f2"
        print(f"[+] 游戏版本: {game_version}, Unity 引擎版本: {unity_version}")

        print("[*] 正在加载 GameAssembly.dll 内存镜像进行特征码扫描...")
        module_data = reader.read(module.base_address, module.size)

        profile_data = {
            "game_version": game_version,
            "unity_version": unity_version,
            "process_name": "ShadowverseWB.exe",
            "module_name": "GameAssembly.dll",
            "gameassembly_sha256": sha256,
        }

        t0 = time.time()
        for key, class_name, namespace in TARGET_CLASSES:
            print(f"[*] 正在扫描类: {namespace}.{class_name} ...")
            classes = find_il2cpp_classes(reader, class_name, namespace, module_name="GameAssembly.dll")
            if not classes:
                # 尝试无命名空间后备
                classes = find_il2cpp_classes(reader, class_name, None, module_name="GameAssembly.dll")
            if not classes:
                print(f"[-] 错误: 未能在内存中找到类 {class_name}")
                return None
            
            # 在 GameAssembly.dll 镜像中反查全局指针 RVA
            found_rvas = []
            for cls_addr in classes:
                pattern = struct.pack("<Q", cls_addr)
                idx = 0
                while True:
                    pos = module_data.find(pattern, idx)
                    if pos < 0:
                        break
                    found_rvas.append(pos)
                    idx = pos + 1

            if not found_rvas:
                print(f"[-] 错误: 未能在 GameAssembly.dll 中定位到 {class_name} 的全局 RVA 槽位！")
                return None

            hex_rva = f"0x{found_rvas[0]:X}"
            profile_data[key] = hex_rva
            print(f"[+] 成功提取 {key}: {hex_rva}")

        elapsed = time.time() - t0
        print(f"[+] 全部特征码提取成功！耗时: {elapsed:.2f} 秒")
        print("\n=== 生成的版本配置 (Version Profile) ===")
        print(json.dumps(profile_data, indent=2, ensure_ascii=False))
        print("=========================================\n")

        if save:
            profiles_dir = project_root / "src" / "tracker" / "version_profiles"
            profiles_dir.mkdir(parents=True, exist_ok=True)
            target_file = profiles_dir / f"{game_version}.json"
            target_file.write_text(json.dumps(profile_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[√] 配置文件已自动保存到: {target_file}")

            # 如果存在本地缓存目录，也同步一份
            base = os.environ.get("LOCALAPPDATA")
            if base:
                cache_dir = Path(base) / "ShadowverseTracker" / "version_profiles"
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / f"{game_version}.json").write_text(
                    json.dumps(profile_data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"[√] 已同步缓存至: {cache_dir / f'{game_version}.json'}")

        return profile_data
    finally:
        reader.close()


def main():
    parser = argparse.ArgumentParser(description="Shadowverse 游戏版本特征码自动提取工具")
    parser.add_argument("--pid", type=int, help="指定游戏进程 PID（可选，默认自动查找）")
    parser.add_argument("--no-save", action="store_true", help="仅打印不保存文件")
    args = parser.parse_args()

    pid = args.pid
    if not pid:
        target_name = "shadowversewb.exe"
        candidates = [p for p in iter_processes() if p.name.casefold() == target_name]
        if not candidates:
            print("[-] 未找到运行中的游戏进程 (ShadowverseWB.exe)！")
            print("    请先启动游戏并进入游戏主界面或对战，然后重新运行本脚本。")
            sys.exit(1)
        pid = candidates[0].pid
        print(f"[+] 自动检测到游戏进程: {candidates[0].name} (PID: {pid})")

    res = extract_profile_from_process(pid, save=not args.no_save)
    if not res:
        print("[-] 提取失败！请确认游戏已加载完成。")
        sys.exit(1)
    print("\n[★] 特征码更新完毕！重启脚本/GUI 即可立即享受新版本内存直读。")


if __name__ == "__main__":
    main()
