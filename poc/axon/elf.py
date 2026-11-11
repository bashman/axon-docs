"""AOT: freeze the graph into a static ELF64 with no libc."""

import os
import struct

from .x86 import lower_x86


def aot_elf(nodes, path, seal: bytes | None = None) -> int:
    if seal is not None and len(seal) != 32:
        raise ValueError("seal must be a 32-byte SHA-256 digest")

    vaddr = 0x400000
    hdr = 64 + 56  # ELF header + 1 program header
    code, data = lower_x86(nodes, lambda clen: vaddr + hdr + clen, as_function=False)
    body = code + data + (seal or b"")
    total = hdr + len(body)
    ehdr = struct.pack(
        "<4sBBBBB7sHHIQQQIHHHHHH",
        b"\x7fELF", 2, 1, 1, 0, 0, b"\0" * 7,  # 64-bit, little endian, SysV
        2, 0x3E, 1,  # ET_EXEC, x86_64, version
        vaddr + hdr, 64, 0,  # entry, phoff, shoff
        0, 64, 56, 1, 0, 0, 0,
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1, 5,  # PT_LOAD, R+X
        0, vaddr, vaddr,  # offset, vaddr, paddr
        total, total, 0x1000,  # filesz, memsz, align
    )
    with open(path, "wb") as f:
        f.write(ehdr + phdr + body)
    os.chmod(path, 0o755)
    return total
