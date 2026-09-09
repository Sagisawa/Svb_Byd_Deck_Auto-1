"""WinRT IDL DSL utilities."""
import enum
from functools import lru_cache
import ctypes
from ctypes import (
    WINFUNCTYPE,
    POINTER,
    c_void_p,
    byref,
    _SimpleCData,
)
from .types import check_hresult, GUID as _GUID, REFGUID


class CtypesEnum(enum.IntEnum):
    """A ctypes-compatible IntEnum superclass."""
    @classmethod
    def from_param(cls, obj):
        return int(obj)


def STDMETHOD(index, name, *argtypes):
    proto = WINFUNCTYPE(check_hresult, *argtypes)
    return proto(index, name)


def define_winrt_com_method(interface, name, *argtypes, retval=None, propget=None, propput=None, vtbl: int = None):
    if vtbl is None:
        vtbl = getattr(interface, "_vtblend", 2) + 1
    if getattr(interface, "_method_defs", None) is None:
        setattr(interface, "_method_defs", [])
    if len(interface.__mro__) > 1 and interface._method_defs is getattr(interface.__mro__[1], "_method_defs", None):
        setattr(interface, "_method_defs", [])

    if retval is not None:
        comfunc = STDMETHOD(vtbl, name, *argtypes, POINTER(retval))
        if len(retval.__mro__) > 1 and retval.__mro__[1] is _SimpleCData:
            def func(this, *args, **kwargs):
                obj = this.astype(interface)
                result = retval()
                comfunc(obj, *args, byref(result), **kwargs)
                return result.value
        else:
            def func(this, *args, **kwargs):
                obj = this.astype(interface)
                result = _new_rtobj(retval)
                comfunc(obj, *args, byref(result), **kwargs)
                return result
        interface._method_defs.append((vtbl, name, comfunc))
        setattr(interface, "_" + name, comfunc)
        setattr(interface, name, func)
    elif propget is not None:
        comgetter = STDMETHOD(vtbl, name, *argtypes, POINTER(propget))
        setattr(interface, name, comgetter)
        interface._method_defs.append((vtbl, name, comgetter))
        if name.startswith("get_"):
            propname = name[4:]
            setter = interface.__dict__.get(propname, None)
            setter = setter.fset if setter is not None else None
            if len(propget.__mro__) > 1 and propget.__mro__[1] is _SimpleCData:
                def getter(this, *args, **kwargs):
                    obj = this.astype(interface)
                    result = propget()
                    comgetter(obj, *args, byref(result), **kwargs)
                    return result.value
            else:
                def getter(this, *args, **kwargs):
                    obj = this.astype(interface)
                    result = _new_rtobj(propget)
                    comgetter(obj, *args, byref(result), **kwargs)
                    return result
            setattr(interface, propname, property(getter, setter))
    elif propput is not None:
        comsetter = STDMETHOD(vtbl, name, *argtypes, propput)
        interface._method_defs.append((vtbl, name, comsetter))
        setattr(interface, name, comsetter)
        if name.startswith("put_"):
            propname = name[4:]
            getter = interface.__dict__.get(propname, None)
            getter = getter.fget if getter is not None else None
            def setter(this, *args, **kwargs):
                obj = this.astype(interface)
                comsetter(obj, *args, **kwargs)
            setattr(interface, propname, property(getter, setter))
    else:
        comfunc = STDMETHOD(vtbl, name, *argtypes)
        def func(this, *args, **kwargs):
            obj = this.astype(interface)
            comfunc(obj, *args, **kwargs)
        interface._method_defs.append((vtbl, name, comfunc))
        setattr(interface, name, funcwrap(func))

    if not hasattr(interface, "_vtblend") or vtbl > interface._vtblend:
        interface._vtblend = vtbl


def funcwrap(f):
    return lambda *args, **kw: f(*args, **kw)


def _new_rtobj(cls, val=None):
    obj = cls()
    if val is not None:
        obj.value = val
    return obj


def GUID(guid_str):
    def decorator(cls):
        cls.GUID = _GUID(guid_str)
        return cls
    return decorator


def runtimeclass(cls):
    return cls


def runtimeclass_add_statics(cls, factory_class_id, factory_interface):
    from .roapi import GetActivationFactory
    factory = GetActivationFactory(factory_class_id).astype(factory_interface)
    cls._factory = factory
    return cls
