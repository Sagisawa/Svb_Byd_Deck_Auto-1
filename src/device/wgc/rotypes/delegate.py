"""WinRT Event delegates."""
import traceback
from ctypes import WINFUNCTYPE, HRESULT, POINTER, Structure, c_void_p, cast, pointer

from .inspectable import IUnknown
from .types import REFGUID, VOIDPP, ULONG, GUID, E_NOINTERFACE, S_OK

_refmap = {}
_DELEGATE_ROOT_REFS = []

_typeof_QueryInterface = WINFUNCTYPE(HRESULT, c_void_p, REFGUID, VOIDPP)
_typeof_AddRef = WINFUNCTYPE(ULONG, c_void_p)
_typeof_Release = WINFUNCTYPE(ULONG, c_void_p)


class _impl_delegate_vtbl(Structure):
    _fields_ = [
        ("QueryInterface", _typeof_QueryInterface),
        ("AddRef", _typeof_AddRef),
        ("Release", _typeof_Release),
        ("Invoke", c_void_p),
    ]


class _impl_delegate(Structure):
    _fields_ = [
        ("lpVtbl", POINTER(_impl_delegate_vtbl)),
    ]


class Delegate(IUnknown):
    def __init__(self, callback, *paramtypes):
        super().__init__()
        self.callback = callback

        def QueryInterface(this, riid, ppv):
            ppv[0] = this
            this_obj = cast(this, c_void_p).value
            _refmap[this_obj] = _refmap.get(this_obj, 0) + 1
            return S_OK

        def AddRef(this):
            _refmap[this] = _refmap.get(this, 0) + 1
            return _refmap[this]

        def Release(this):
            _refmap[this] = _refmap.get(this, 1) - 1
            if _refmap[this] <= 0:
                _refmap.pop(this, None)
                return 0
            return _refmap[this]

        def Invoke(this, *args):
            try:
                self.callback(*args)
            except Exception:
                traceback.print_exc()
            return S_OK

        self._c_QI = _typeof_QueryInterface(QueryInterface)
        self._c_AddRef = _typeof_AddRef(AddRef)
        self._c_Release = _typeof_Release(Release)
        self._c_Invoke = WINFUNCTYPE(HRESULT, c_void_p, *paramtypes)(Invoke)

        self._vtbl = _impl_delegate_vtbl(
            self._c_QI,
            self._c_AddRef,
            self._c_Release,
            cast(self._c_Invoke, c_void_p),
        )
        self._impl = _impl_delegate(pointer(self._vtbl))
        self.value = cast(pointer(self._impl), c_void_p).value
        # 关键：在全局列表中永久持有回调函数与虚表结构，避免被 GC 回收后产生悬垂 C 回调！
        _DELEGATE_ROOT_REFS.append(
            (
                self,
                self._vtbl,
                self._impl,
                self._c_QI,
                self._c_AddRef,
                self._c_Release,
                self._c_Invoke,
                callback,
            )
        )
    @classmethod
    def from_param(cls, obj):
        if hasattr(obj, "value"):
            return obj.value
        return obj
