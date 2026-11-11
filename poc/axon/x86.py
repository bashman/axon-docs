"""x86_64 backend: fixed instruction templates per node."""

import struct


def lower_x86(nodes, data_base_fn, as_function: bool):
    """
    Lower the graph to raw x86_64. Layout: [code][data].
    as_function=True  -> JIT: the final 'exit' becomes 'ret' (do not kill the host).
    as_function=False -> AOT: 'exit' is a real sys_exit.
    data_base_fn(code_len) -> absolute address where the data block will live.
    """
    datas = [nd[2] for nd in nodes if nd[0] == "write"]

    def emit(base):
        code, k = bytearray(), 0
        if as_function:
            code += b"\x53"  # push rbx (align stack)
        for nd in nodes:
            if nd[0] == "write":
                _, fd, data = nd
                addr = base + sum(len(d) for d in datas[:k])
                k += 1
                code += b"\xb8" + struct.pack("<I", 1)  # mov eax, 1 (sys_write)
                code += b"\xbf" + struct.pack("<I", fd)  # mov edi, fd
                code += b"\x48\xbe" + struct.pack("<Q", addr)  # mov rsi, addr
                code += b"\xba" + struct.pack("<I", len(data))  # mov edx, len
                code += b"\x0f\x05"  # syscall
            elif nd[0] == "exit":
                if as_function:
                    code += b"\x5b\xc3"  # pop rbx ; ret
                else:
                    code += b"\xb8" + struct.pack("<I", 60)  # mov eax, 60 (sys_exit)
                    code += b"\xbf" + struct.pack("<I", nd[1])  # mov edi, code
                    code += b"\x0f\x05"  # syscall
        return bytes(code)

    probe = emit(0)  # code size does not depend on addresses
    code = emit(data_base_fn(len(probe)))
    return code, b"".join(datas)
