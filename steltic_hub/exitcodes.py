"""What a bare exit code means when a module process dies without a Python traceback.

A native crash (PyTorch, onnxruntime, pdfium, OpenSees) ends the process with a Windows NTSTATUS
code -- 3221225477 is 0xC0000005, an access violation -- or, on POSIX, a negative signal number.
Nothing Python-side runs, so the log just stops and the run says "exited 3221225477". This turns
the number into a sentence and, where it matters, a next step.
"""
from __future__ import annotations

_WINDOWS = {
    0xC0000005: ("access violation", "a native library crashed (PyTorch / onnxruntime / pdfium / OpenSees) -- most often "
                                     "memory on a large input, or one bad page"),
    0xC0000017: ("no memory", "the process ran out of memory"),
    0xC00000FD: ("stack overflow", "a native library recursed too deep"),
    0xC0000135: ("DLL not found", "a native library is missing from the module environment -- Reinstall the module"),
    0xC0000142: ("DLL init failed", "a native library failed to initialise -- Reinstall the module, or check Smart App Control"),
    0xC0000374: ("heap corruption", "a native library corrupted its heap"),
    0xC0000409: ("fail-fast / stack buffer overrun", "a native library aborted on a safety check"),
    0xC000013A: ("Ctrl+C", "the process was interrupted"),
}
_POSIX = {
    -9: ("SIGKILL", "the process was killed -- on Linux usually the kernel's out-of-memory killer"),
    -11: ("SIGSEGV", "a native library crashed (segmentation fault)"),
    -6: ("SIGABRT", "a native library aborted"),
    -7: ("SIGBUS", "a native library crashed (bus error)"),
    -15: ("SIGTERM", "the process was stopped"),
}

def code_name(rc: int) -> str:
    """The short form of an exit code -- `0xC0000005`, or `signal 11` on POSIX. A one-line message
    (the retry notice) names the code without repeating the whole explanation."""
    return f"signal {-rc}" if rc < 0 else f"0x{rc & 0xFFFFFFFF:08X}"


def explain(rc: int | None, crash_hint: str = "") -> str | None:
    """A sentence for an exit code that means a native crash or a kill; None for ordinary codes.
    `crash_hint` is the tab's own next step from its manifest (run.crash_hint), appended when present."""
    if rc is None or rc in (0, 1, 2):
        return None
    hit = _POSIX.get(rc) if rc < 0 else _WINDOWS.get(rc & 0xFFFFFFFF)
    if not hit:
        return None
    code = code_name(rc)
    name, meaning = hit
    text = f"The module died with exit {rc} ({code}, {name}): {meaning}. No Python traceback exists for this kind of failure."
    if crash_hint:
        text += " " + crash_hint.strip()
    return text
