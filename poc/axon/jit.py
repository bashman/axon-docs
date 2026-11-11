"""JIT: run generated x86_64 from an executable in-memory page."""

import ctypes
import mmap
import sys

from .x86 import lower_x86


def _page_aligned_size(size: int) -> int:
    return max(mmap.PAGESIZE, (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE * mmap.PAGESIZE)


def _make_executable(address: int, size: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    mprotect = libc.mprotect
    mprotect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    mprotect.restype = ctypes.c_int
    if mprotect(address, size, mmap.PROT_READ | mmap.PROT_EXEC) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "mprotect failed")


def jit_run(nodes):
    probe_code, data = lower_x86(nodes, lambda clen: clen, as_function=True)
    size = _page_aligned_size(len(probe_code) + len(data))
    mem = mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE)
    base = ctypes.addressof(ctypes.c_char.from_buffer(mem))
    code, data = lower_x86(nodes, lambda clen: base + clen, as_function=True)
    mem.write(code + data)
    _make_executable(base, size)
    sys.stdout.flush()
    ctypes.CFUNCTYPE(None)(base)()  # jump straight into generated code
    return code, data
