"""Classic Mac file metadata: type/creator codes and resource forks, read through extended
attributes (on exFAT drives macOS reads them from the "._" companion files)."""
import ctypes
import ctypes.util
import os
import sys


def make_xattr_reader():
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.getxattr
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int]
        fn.restype = ctypes.c_ssize_t
        NOFOLLOW = 0x0001

        def get(path, name, size_only=False):
            p, n = os.fsencode(path), name.encode()
            size = fn(p, n, None, 0, 0, NOFOLLOW)
            if size < 0:
                return None
            if size_only:
                return size
            buf = ctypes.create_string_buffer(size)
            got = fn(p, n, buf, size, 0, NOFOLLOW)
            return buf.raw[:got] if got >= 0 else None
        return get

    def get(path, name, size_only=False):          # Linux, for testing
        try:
            v = os.getxattr(path, "user." + name, follow_symlinks=False)
        except (OSError, AttributeError):
            return None
        return len(v) if size_only else v
    return get


def mac_codes(xattr, path):
    fi = xattr(path, "com.apple.FinderInfo")
    t = c = ""
    if fi and len(fi) >= 8 and fi[:4] != b"\0\0\0\0":
        t, c = fi[:4].decode("mac_roman"), fi[4:8].decode("mac_roman")
    rs = xattr(path, "com.apple.ResourceFork", size_only=True) or 0
    return t, c, rs


# ---------- 1. scan ----------
