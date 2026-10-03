"""Supported-game-version loading and safe automatic profile updates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import struct
from urllib.request import Request, urlopen

from .memory.win32 import ProcessReader


class UnsupportedGameVersion(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionProfile:
    game_version: str
    unity_version: str
    process_name: str
    module_name: str
    gameassembly_sha256: str
    battle_model_class_pointer_rva: int
    deck_info_class_pointer_rva: int
    practice_battle_model_class_pointer_rva: int
    auto_compatible: bool = False
    # Some regional clients retain the shared BattleRootMpo model but strip
    # the presentation/deck type-info globals used by the Steam build.  A
    # zero RVA tells discovery to resolve live classes from runtime names.
    dynamic_discovery: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "VersionProfile":
        raw_dynamic = value.get("dynamic_discovery", False)
        dynamic_discovery = (
            raw_dynamic
            if isinstance(raw_dynamic, bool)
            else str(raw_dynamic).strip().casefold() in {"1", "true", "yes", "on"}
        )
        return cls(
            game_version=str(value["game_version"]),
            unity_version=str(value["unity_version"]),
            process_name=str(value["process_name"]),
            module_name=str(value["module_name"]),
            gameassembly_sha256=str(value["gameassembly_sha256"]).upper(),
            battle_model_class_pointer_rva=int(str(value["battle_model_class_pointer_rva"]), 0),
            deck_info_class_pointer_rva=int(str(value["deck_info_class_pointer_rva"]), 0),
            practice_battle_model_class_pointer_rva=int(
                str(value["practice_battle_model_class_pointer_rva"]), 0
            ),
            dynamic_discovery=dynamic_discovery,
        )


def load_profiles() -> tuple[VersionProfile, ...]:
    profiles: dict[str, VersionProfile] = {}
    local_dir = Path(__file__).resolve().parent / "version_profiles"
    if local_dir.is_dir():
        for item in local_dir.glob("*.json"):
            profile = VersionProfile.from_dict(json.loads(item.read_text(encoding="utf-8")))
            profiles[profile.gameassembly_sha256] = profile
    else:
        try:
            package = resources.files(__package__ + ".version_profiles") if __package__ else resources.files("src.tracker.version_profiles")
            for item in package.iterdir():
                if item.name.endswith(".json"):
                    profile = VersionProfile.from_dict(json.loads(item.read_text(encoding="utf-8")))
                    profiles[profile.gameassembly_sha256] = profile
        except Exception:
            pass
    cache = profile_cache_dir()
    if cache.is_dir():
        for item in cache.glob("*.json"):
            try:
                profile = VersionProfile.from_dict(json.loads(item.read_text(encoding="utf-8")))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            profiles[profile.gameassembly_sha256] = profile
    return tuple(profiles.values())


def profile_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ShadowverseTracker" / "version_profiles"
    return Path.home() / ".shadowverse_tracker" / "version_profiles"


PROFILE_API_URL = (
    "https://api.github.com/repos/DaydreamStarRiver/ShadowverseTracker/contents/"
    "src/shadowverse_tracker/version_profiles"
)


def sync_remote_profiles(*, timeout: float = 5.0) -> int:
    """Download vetted profile JSON files from the project repository."""
    request = Request(
        PROFILE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ShadowverseTracker"},
    )
    with urlopen(request, timeout=timeout) as response:
        listing = json.loads(response.read().decode("utf-8"))
    if not isinstance(listing, list):
        raise ValueError("版本配置服务器返回了无效目录")
    cache = profile_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    updated = 0
    for item in listing:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        download_url = item.get("download_url")
        if not name.endswith(".json") or not isinstance(download_url, str):
            continue
        with urlopen(Request(download_url, headers={"User-Agent": "ShadowverseTracker"}), timeout=timeout) as response:
            raw = response.read()
        value = json.loads(raw.decode("utf-8"))
        VersionProfile.from_dict(value)
        target = cache / name
        if target.is_file() and target.read_bytes() == raw:
            continue
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, target)
        updated += 1
    return updated


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


_CLASS_IDENTITIES = (
    ("battle_model_class_pointer_rva", "Wizard2.Presentation.Battle", "BattleModel"),
    ("deck_info_class_pointer_rva", "Wizard2.Domain.DeckInfoData", "DeckInfo"),
    ("practice_battle_model_class_pointer_rva", "Wizard2.Presentation.Practice", "PracticeBattleModel"),
)


def _version_key(profile: VersionProfile) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"\d+", profile.game_version))


def _core_classes_match(reader: ProcessReader, module, profile: VersionProfile) -> bool:
    """Confirm that the latest profile's three global class pointers are intact."""
    # There is no safe pointer identity to validate for a dynamic profile;
    # exact GameAssembly hashing remains the trust boundary and live object
    # discovery performs structural checks before decoding anything.
    if any(getattr(profile, field) <= 0 for field, _namespace, _name in _CLASS_IDENTITIES):
        return False
    try:
        for field, expected_namespace, expected_name in _CLASS_IDENTITIES:
            class_address = reader.read_u64(module.base_address + getattr(profile, field))
            if not class_address:
                return False
            name = reader.read_c_string(reader.read_u64(class_address + 0x10))
            namespace = reader.read_c_string(reader.read_u64(class_address + 0x18))
            if name != expected_name or namespace != expected_namespace:
                return False
    except (OSError, ValueError):
        return False
    return True


def _auto_compatible_profile(
    reader: ProcessReader,
    module,
    profiles: tuple[VersionProfile, ...],
    actual_hash: str,
) -> VersionProfile | None:
    """Reuse a recent profile only after exact IL2CPP class identity checks."""
    for profile in sorted(profiles, key=_version_key, reverse=True):
        if _core_classes_match(reader, module, profile):
            return replace(
                profile,
                game_version=f"自动兼容 {actual_hash[:12]}",
                gameassembly_sha256=actual_hash,
                auto_compatible=True,
            )
    return None


def _get_file_version(file_path: str | Path) -> str:
    """读取 Windows 可执行文件的文件版本号 (如 1.9.11.19463)."""
    try:
        import ctypes
        from ctypes import wintypes

        path_str = str(file_path)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path_str, None)
        if not size:
            return ""
        res = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path_str, 0, size, res):
            return ""
        u_len = wintypes.UINT()
        l_ptr = ctypes.c_void_p()
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
        sub_block = f"\\StringFileInfo\\{buf[0]:04x}{buf[1]:04x}\\ProductVersion"
        if ctypes.windll.version.VerQueryValueW(
            res, sub_block, ctypes.byref(l_ptr), ctypes.byref(u_len)
        ):
            return ctypes.wstring_at(l_ptr.value)
    except Exception:
        pass
    return ""


def _dynamically_extract_profile(
    reader: ProcessReader,
    module,
    actual_hash: str,
) -> VersionProfile | None:
    """当所有静态配置均失效时，直接在运行中的游戏内存中动态提取 IL2CPP 类全局 RVA 并持久化配置."""
    import logging

    log = logging.getLogger(__name__)
    try:
        from .memory.discovery import find_il2cpp_classes

        log.info(f"【内存直读】正在为未识别的 GameAssembly.dll ({actual_hash[:12]}) 动态分析特征码...")
        module_data = reader.read(module.base_address, module.size)
        rvas: dict[str, int] = {}
        for field, expected_namespace, expected_name in _CLASS_IDENTITIES:
            classes = find_il2cpp_classes(
                reader, expected_name, expected_namespace, module_name=module.name
            )
            if not classes:
                classes = find_il2cpp_classes(reader, expected_name, None, module_name=module.name)
            if not classes:
                log.warning(f"【内存直读】动态分析失败：未能在内存中定位到 {expected_name}")
                return None

            found_rvas: list[int] = []
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
                log.warning(f"【内存直读】动态分析失败：未能在模块中定位到 {expected_name} 的全局槽位")
                return None
            rvas[field] = found_rvas[0]

        game_version = f"auto-{actual_hash[:8]}"
        unity_version = "2022.3.62f2"
        try:
            exe_path = reader.module("ShadowverseWB.exe").path
            gv = _get_file_version(exe_path)
            if gv:
                game_version = gv
        except Exception:
            pass

        try:
            unity_path = reader.module("UnityPlayer.dll").path
            uv = _get_file_version(unity_path)
            if uv:
                unity_version = uv
        except Exception:
            pass

        profile_dict = {
            "game_version": game_version,
            "unity_version": unity_version,
            "process_name": "ShadowverseWB.exe",
            "module_name": module.name,
            "gameassembly_sha256": actual_hash,
            "battle_model_class_pointer_rva": f"0x{rvas['battle_model_class_pointer_rva']:X}",
            "deck_info_class_pointer_rva": f"0x{rvas['deck_info_class_pointer_rva']:X}",
            "practice_battle_model_class_pointer_rva": f"0x{rvas['practice_battle_model_class_pointer_rva']:X}",
            "auto_compatible": True,
        }

        # 写入本地持久化目录
        raw_json = json.dumps(profile_dict, indent=2, ensure_ascii=False)
        cache = profile_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{game_version}.json").write_text(raw_json, encoding="utf-8")

        local_dir = Path(__file__).resolve().parent / "version_profiles"
        if local_dir.is_dir():
            (local_dir / f"{game_version}.json").write_text(raw_json, encoding="utf-8")

        log.info(
            f"【内存直读】已成功动态识别新游戏版本 {game_version} 并自动保存特征码配置！"
        )
        return VersionProfile.from_dict(profile_dict)
    except Exception as exc:
        log.warning(f"【内存直读】动态分析过程发生异常: {exc}")
        return None


def verify_process_version(reader: ProcessReader) -> VersionProfile:
    profiles = load_profiles()
    module_names = {profile.module_name.casefold() for profile in profiles}
    if len(module_names) != 1:
        raise RuntimeError("version profiles disagree on module name")
    module = reader.module(profiles[0].module_name)
    actual_hash = sha256_file(module.path)
    for profile in profiles:
        if profile.gameassembly_sha256 == actual_hash:
            return profile
    update_error = ""
    try:
        sync_remote_profiles()
        refreshed = load_profiles()
        for profile in refreshed:
            if profile.gameassembly_sha256 == actual_hash:
                return profile
        profiles = refreshed
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        update_error = f"；自动更新失败：{exc}"
    compatible = _auto_compatible_profile(reader, module, profiles, actual_hash)
    if compatible is not None:
        return compatible
    # 动态分析自愈机制：若已知配置文件未命中且远程未就绪，自动动态提取当前运行游戏的特征码
    dynamic = _dynamically_extract_profile(reader, module, actual_hash)
    if dynamic is not None:
        return dynamic
    raise UnsupportedGameVersion(
        f"不支持当前 GameAssembly.dll（SHA-256: {actual_hash}）{update_error}"
    )


KNOWN_GAME_PROCESS_NAMES = (
    "ShadowverseWB.exe",
    "MuMu模拟器x影之诗高清版.exe",
)


def check_game_profile_status() -> dict[str, object]:
    """检查当前运行中的游戏进程与特征码配置匹配状态."""
    from .memory.win32 import iter_processes

    target_names = {name.casefold() for name in KNOWN_GAME_PROCESS_NAMES}
    candidates = [p for p in iter_processes() if p.name.casefold() in target_names]
    if not candidates:
        return {
            "status": "not_running",
            "pid": None,
            "game_version": "",
            "sha256": "",
            "message": "○ 游戏未运行",
        }

    pid = candidates[0].pid
    proc_name = candidates[0].name
    try:
        reader = ProcessReader(pid)
        try:
            module = reader.module("GameAssembly.dll")
            actual_hash = sha256_file(module.path)
            profiles = load_profiles()
            for profile in profiles:
                if profile.gameassembly_sha256 == actual_hash:
                    return {
                        "status": "matched",
                        "pid": pid,
                        "game_version": profile.game_version,
                        "sha256": actual_hash,
                        "message": f"● 特征码匹配 ({profile.game_version})",
                    }
            compatible = _auto_compatible_profile(reader, module, profiles, actual_hash)
            if compatible is not None:
                return {
                    "status": "matched",
                    "pid": pid,
                    "game_version": compatible.game_version,
                    "sha256": actual_hash,
                    "message": f"● 特征码兼容 ({compatible.game_version})",
                }
            gv = ""
            try:
                gv = _get_file_version(reader.module(proc_name).path)
            except Exception:
                pass
            return {
                "status": "mismatch",
                "pid": pid,
                "game_version": gv or "新版本",
                "sha256": actual_hash,
                "message": f"▲ 特征码未适配 ({gv or '新版本'})",
            }
        finally:
            reader.close()
    except Exception as exc:
        return {
            "status": "error",
            "pid": pid,
            "game_version": "",
            "sha256": "",
            "message": f"○ 检测异常: {exc}",
        }


def extract_and_save_profile_for_process(
    pid: int | None = None,
) -> tuple[bool, str, dict[str, object] | None]:
    """对游戏进程动态提取 IL2CPP 特征码并自动写入配置文件."""
    from .memory.win32 import iter_processes

    if pid is None:
        target_names = {name.casefold() for name in KNOWN_GAME_PROCESS_NAMES}
        candidates = [p for p in iter_processes() if p.name.casefold() in target_names]
        if not candidates:
            return False, "未找到运行中的游戏进程 (ShadowverseWB.exe / MuMu模拟器x影之诗高清版.exe)", None
        pid = candidates[0].pid

    try:
        reader = ProcessReader(pid)
        try:
            module = reader.module("GameAssembly.dll")
            actual_hash = sha256_file(module.path)
            prof = _dynamically_extract_profile(reader, module, actual_hash)
            if prof is not None:
                return (
                    True,
                    f"成功提取并生成版本配置 ({prof.game_version})",
                    {
                        "game_version": prof.game_version,
                        "unity_version": prof.unity_version,
                        "gameassembly_sha256": prof.gameassembly_sha256,
                        "battle_model_class_pointer_rva": hex(
                            prof.battle_model_class_pointer_rva
                        ),
                        "deck_info_class_pointer_rva": hex(prof.deck_info_class_pointer_rva),
                        "practice_battle_model_class_pointer_rva": hex(
                            prof.practice_battle_model_class_pointer_rva
                        ),
                    },
                )
            return (
                False,
                "动态解析核心类指针或 RVA 槽位失败，请确认游戏已进入主界面",
                None,
            )
        finally:
            reader.close()
    except Exception as exc:
        return False, f"提取过程发生异常: {exc}", None
