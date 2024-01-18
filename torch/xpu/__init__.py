r"""
This package is lazily initialized, so you can always import it.
"""
import threading
from functools import lru_cache
from typing import Any, Dict, Optional, Union

import torch
import torch._C
from .. import device as _device
from ._utils import _dummy_type, _get_device_index
from .streams import Event, Stream

_initialized = False
_initialization_lock = threading.Lock()
_is_in_bad_fork = getattr(torch._C, "_xpu_isInBadFork", lambda: False)
_device_t = Union[_device, str, int, None]
has_half: bool = True


def _is_compiled() -> bool:
    r"""Return true if compile with XPU support."""
    return torch._C._has_xpu


if _is_compiled():
    _XpuDeviceProperties = torch._C._XpuDeviceProperties
    _exchange_device = torch._C._xpu_exchangeDevice
    _maybe_exchange_device = torch._C._xpu_maybeExchangeDevice
else:
    # Define dummy if PyTorch was compiled without XPU
    _XpuDeviceProperties = _dummy_type("_XpuDeviceProperties")  # type: ignore[assignment, misc]

    def _exchange_device(device: int) -> int:
        raise NotImplementedError("PyTorch was compiled without XPU support")

    def _maybe_exchange_device(device: int) -> int:
        raise NotImplementedError("PyTorch was compiled without XPU support")


@lru_cache(maxsize=1)
def device_count() -> int:
    r"""Return the number of XPU device available."""
    if not _is_compiled():
        return 0
    return torch._C._xpu_getDeviceCount()


def is_available() -> bool:
    r"""Return a bool indicating if XPU is currently available."""
    # This function nerver throws.
    return device_count() > 0


def is_bf16_supported():
    r"""Return a bool indicating if the current XPU device supports dtype bfloat16."""
    return True


def is_initialized():
    r"""Return whether PyTorch's XPU state has been initialized."""
    return _initialized and not _is_in_bad_fork()


def init():
    r"""Initialize PyTorch's XPU state.
    This is a Python API about lazy initialization that avoids initializing
    XPU until the first time it is accessed. Does nothing if the XPU state is
    already initialized.
    """
    _lazy_init()


def _lazy_init():
    global _initialized
    if is_initialized():
        return
    with _initialization_lock:
        # This test was was protected via GIL. Double-check whether XPU has
        # already been initialized.
        if is_initialized():
            return
        # Stop promptly upon encountering a bad fork error.
        if _is_in_bad_fork():
            raise RuntimeError(
                "Cannot re-initialize XPU in forked subprocess. To use XPU with "
                "multiprocessing, you must use the 'spawn' start method"
            )
        if not _is_compiled():
            raise AssertionError("Torch not compiled with XPU enabled")
        # This function inits XPU backend and detects bad fork processing.
        torch._C._xpu_init()
        _initialized = True


class _DeviceGuard:
    def __init__(self, index: int):
        self.idx = index
        self.prev_idx = -1

    def __enter__(self):
        self.prev_idx = torch.xpu._exchange_device(self.idx)

    def __exit__(self, type: Any, value: Any, traceback: Any):
        self.idx = torch.xpu._maybe_exchange_device(self.prev_idx)
        return False


class device:
    r"""Context-manager that changes the selected device.

    Args:
        device (torch.device or int or str): device index to select. It's a no-op if
            this argument is a negative integer or ``None``.
    """

    def __init__(self, device: Any):
        self.idx = _get_device_index(device, optional=True)
        self.prev_idx = -1

    def __enter__(self):
        self.prev_idx = torch.xpu._exchange_device(self.idx)

    def __exit__(self, type: Any, value: Any, traceback: Any):
        self.idx = torch.xpu._maybe_exchange_device(self.prev_idx)
        return False


class device_of(device):
    r"""Context-manager that changes the current device to that of given object.

    You can use both tensors and storages as arguments. If a given object is
    not allocated on a XPU, this is a no-op.

    Args:
        obj (Tensor or Storage): object allocated on the selected device.
    """

    def __init__(self, obj):
        idx = obj.get_device() if obj.is_xpu else -1
        super().__init__(idx)


def set_device(device: _device_t) -> None:
    r"""Set the current device.

    Args:
        device (torch.device or int or str): selected device. This function is a
            no-op if this argument is negative.
    """
    _lazy_init()
    device = _get_device_index(device)
    if device >= 0:
        torch._C._xpu_setDevice(device)


def get_device_name(device: Optional[_device_t] = None) -> str:
    r"""Get the name of a device.

    Args:
        device (torch.device or int or str, optional): device for which to
            return the name. This function is a no-op if this argument is a
            negative integer. It uses the current device, given by :func:`~torch.xpu.current_device`,
            if :attr:`device` is ``None`` (default).

    Returns:
        str: the name of the device
    """
    return get_device_properties(device).name


def get_device_capability(device: Optional[_device_t] = None) -> Dict[str, Any]:
    r"""Get the xpu capability of a device.

    Args:
        device (torch.device or int or str, optional): device for which to
            return the device capability. This function is a no-op if this
            argument is a negative integer. It uses the current device, given by
            :func:`~torch.xpu.current_device`, if :attr:`device` is ``None``
            (default).

    Returns:
        Dict[str, Any]: the xpu capability dictionary of the device
    """
    prop = get_device_properties(device)
    return {
        "max_work_group_size": prop.max_work_group_size,
        "max_num_sub_groups": prop.max_num_sub_groups,
        "sub_group_sizes": prop.sub_group_sizes,
    }


def get_device_properties(device: _device_t) -> _XpuDeviceProperties:
    r"""Get the properties of a device.

    Args:
        device (torch.device or int or str): device for which to return the
            properties of the device.

    Returns:
        _XpuDeviceProperties: the properties of the device
    """
    _lazy_init()
    device = _get_device_index(device, optional=True)
    if device < 0 or device >= device_count():
        raise AssertionError("Invalid device index")
    return _get_device_properties(device)  # type: ignore[name-defined]  # noqa: F821


def current_device() -> int:
    r"""Return the index of a currently selected device."""
    _lazy_init()
    return torch._C._xpu_getDevice()


def _get_device(device: Union[int, str, torch.device]) -> torch.device:
    r"""Return the torch.device type object from the passed in device.

    Args:
        device (torch.device or int or str): selected device.
    """
    if isinstance(device, str):
        device = torch.device(device)
    elif isinstance(device, int):
        device = torch.device("xpu", device)
    return device


class StreamContext:
    r"""Context-manager that selects a given stream.

    All XPU kernels queued within its context will be enqueued on a selected
    stream.

    Args:
        Stream (Stream): selected stream. This manager is a no-op if it's
            ``None``.
    .. note:: Streams are per-device.
    """
    cur_stream: Optional["torch.xpu.Stream"]

    def __init__(self, stream: Optional["torch.xpu.Stream"]):
        assert not torch.jit.is_scripting()
        self.stream = stream
        self.idx = _get_device_index(None, True)
        if self.idx is None:
            self.idx = -1

    def __enter__(self):
        cur_stream = self.stream
        if cur_stream is None or self.idx == -1:
            return
        self.src_prev_stream = torch.xpu.current_stream(None)

        # If the stream is not on the current device, then set the current stream on the device
        if self.src_prev_stream.device != cur_stream.device:
            with device(cur_stream.device):
                self.dst_prev_stream = torch.xpu.current_stream(cur_stream.device)
        torch.xpu.set_stream(cur_stream)

    def __exit__(self, type: Any, value: Any, traceback: Any):
        cur_stream = self.stream
        if cur_stream is None or self.idx == -1:
            return

        # Reset the stream on the original device and destination device
        if self.src_prev_stream.device != cur_stream.device:
            torch.xpu.set_stream(self.dst_prev_stream)
        torch.xpu.set_stream(self.src_prev_stream)


def stream(stream: Optional["torch.xpu.Stream"]) -> StreamContext:
    r"""Wrap around the Context-manager StreamContext that selects a given stream.

    Arguments:
        stream (Stream): selected stream. This manager is a no-op if it's ``None``.
    """
    return StreamContext(stream)


def _set_stream_by_id(stream_id, device_index, device_type):
    r"""set stream specified by the stream id, device index and device type

    Args: stream_id (int): not visible to the user, used to assigned to the specific stream.
          device_index (int): selected device index.
          device_type (int): selected device type.
    """
    torch._C._xpu_setStream(
        stream_id=stream_id,
        device_index=device_index,
        device_type=device_type,
    )


def set_stream(stream: Stream):
    r"""Set the current stream.This is a wrapper API to set the stream.
        Usage of this function is discouraged in favor of the ``stream``
        context manager.

    Args:
        stream (Stream): selected stream. This function is a no-op
            if this argument is ``None``.
    """
    if stream is None:
        return
    _lazy_init()
    _set_stream_by_id(
        stream_id=stream.stream_id,
        device_index=stream.device_index,
        device_type=stream.device_type,
    )


def current_stream(device: Optional[_device_t] = None) -> Stream:
    r"""Return the currently selected :class:`Stream` for a given device.

    Args:
        device (torch.device or int, optional): selected device. Returns
            the currently selected :class:`Stream` for the current device, given
            by :func:`~torch.xpu.current_device`, if :attr:`device` is ``None``
            (default).
    """
    _lazy_init()
    streamdata = torch._C._xpu_getCurrentStream(
        _get_device_index(device, optional=True)
    )
    return Stream(
        stream_id=streamdata[0], device_index=streamdata[1], device_type=streamdata[2]
    )


def synchronize(device: _device_t = None) -> None:
    r"""Wait for all kernels in all streams on a XPU device to complete.

    Args:
        device (torch.device or int, optional): device for which to synchronize.
            It uses the current device, given by :func:`~torch.xpu.current_device`,
            if :attr:`device` is ``None`` (default).
    """
    _lazy_init()
    device = _get_device_index(device)
    return torch._C._xpu_synchronize(device)


__all__ = [
    "Event",
    "Stream",
    "StreamContext",
    "current_device",
    "current_stream",
    "device",
    "device_of",
    "device_count",
    "get_device_capability",
    "get_device_name",
    "get_device_properties",
    "get_stream",
    "has_half",
    "init",
    "is_available",
    "is_bf16_supported",
    "is_initialized",
    "set_device",
    "stream",
    "streams",
    "synchronize",
]
