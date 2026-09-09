"""WinRT types definition."""
from ctypes import *
from ctypes.wintypes import *
import ctypes

HRESULT = LONG
VOIDPP = POINTER(c_void_p)

S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
E_FAIL = -2147467259              # 0x80004005
E_NOTIMPL = -2147467263           # 0x80004001
E_NOINTERFACE = -2147467262       # 0x80004002
E_BOUNDS = -2147483637            # 0x8000000B


def check_hresult(hr):
    if (hr & 0x80000000) != 0:
        if hr == RPC_E_CHANGED_MODE:
            return hr
        if hr == E_NOTIMPL:
            raise NotImplementedError
        elif hr == E_NOINTERFACE:
            raise TypeError("E_NOINTERFACE")
        elif hr == E_BOUNDS:
            raise IndexError
        e = OSError()
        e.errno = hr
        e.winerror = hr & 0xFFFFFFFF
        e.strerror = f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
        raise e
    return hr


class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]

    def __init__(self, guid_str=None):
        super().__init__()
        if guid_str:
            import uuid
            u = uuid.UUID(guid_str)
            self.Data1 = u.time_low
            self.Data2 = u.time_mid
            self.Data3 = u.time_hi_version
            for i in range(8):
                self.Data4[i] = u.bytes[8 + i]

    def __str__(self):
        return f"{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{''.join(f'{b:02x}' for b in self.Data4[:2])}-{''.join(f'{b:02x}' for b in self.Data4[2:])}"

    def __repr__(self):
        return f"GUID('{self}')"

    def __eq__(self, other):
        if isinstance(other, GUID):
            return bytes(self) == bytes(other)
        return False


REFGUID = POINTER(GUID)
