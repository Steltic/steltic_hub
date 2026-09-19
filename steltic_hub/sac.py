"""Windows 11 Smart App Control -- the one machine setting that silently breaks the native stacks.

Smart App Control (SAC) is a WDAC policy that refuses to load executables and DLLs that are neither
Authenticode-signed nor known-good in Microsoft's reputation graph. PyTorch (the PDF converter),
OpenSees (the design engines), onnxruntime and most scientific-Python native libraries are unsigned,
so with SAC *on* they fail at import with

    OSError: [WinError 4551] An Application Control policy has blocked this file.

The catch: a fresh Windows 11 machine runs SAC in *evaluation* mode, where nothing is blocked, and
flips itself to *on* (often at a reboot) once it decides the machine "does not need" unsigned
software -- so a converter that worked yesterday fails today. There is no per-file allow-list.
The only settings are On and Off, and Off is one-way: it cannot be turned back on without resetting
Windows. The hub does not change it; it says clearly when it is the reason.

State lives in the registry:
  HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\\VerifiedAndReputablePolicyState
  0 = off, 1 = on, 2 = evaluation.
"""
from __future__ import annotations
import sys

_KEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
_VALUE = "VerifiedAndReputablePolicyState"
STATES = {0: "off", 1: "on", 2: "evaluation"}

BLOCK_MARKERS = ("WinError 4551", "Application Control policy has blocked", "Smart App Control")

ADVICE = ("Windows 11 Smart App Control is ON and blocks unsigned native libraries (PyTorch for the PDF "
          "converter, OpenSees for the design engines, onnxruntime). It has no per-file allow-list. To run "
          "these modules on this PC: Windows Security > App & browser control > Smart App Control settings > "
          "Off (one-way: it cannot be re-enabled without resetting Windows). Query, the hub and the "
          "MOCK design model keep working with it on.")


def state() -> str | None:
    """'off' | 'on' | 'evaluation' on Windows 11; None elsewhere or when the key is absent."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as k:
            v, _t = winreg.QueryValueEx(k, _VALUE)
        return STATES.get(int(v), f"unknown ({v})")
    except OSError:
        return None


def looks_blocked(line: str) -> bool:
    """Does this process output line say Application Control refused a file?"""
    return any(m in line for m in BLOCK_MARKERS)


def hint(current: str | None = None) -> str:
    """The sentence to append to a run that hit a 4551 (or to a Modules-page banner)."""
    current = state() if current is None else current
    if current == "on":
        return ADVICE
    if current == "evaluation":
        return ("Windows 11 Smart App Control is in evaluation mode: nothing is blocked yet, but it can switch itself "
                "on at a reboot, after which PyTorch / OpenSees / onnxruntime fail with WinError 4551. " + ADVICE)
    return ("An Application Control policy on this PC blocked an unsigned library (WinError 4551): Smart App "
            "Control or a WDAC policy from your organisation. " + ADVICE)
