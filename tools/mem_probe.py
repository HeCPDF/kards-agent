#!/usr/bin/env python3
"""Stage-0 read-only memory probe for KARDS.

STRICTLY READ-ONLY: the handle is opened with PROCESS_QUERY_INFORMATION |
PROCESS_VM_READ only, and the sole write-ish API used is ReadProcessMemory.
No injection, no WriteProcessMemory, no CreateRemoteThread, no hooking.

Target build (verified 3 ways - idmap symbol bytes, AkardsGameState_VFT
contents, and IDA's copy of execgetKreditSlotBySide):
    kards-Win64-Shipping.exe
    md5 395e470f06837f6e60ce5c53c6df2a22
    160489984 bytes, imagebase 0x140000000, SizeOfImage 0x9CC8000
Offsets come from the Dumper-7 Dumpspace dump of exactly that build.

Usage:
  python mem_probe.py                     # attach, validate, print one report
  python mem_probe.py --watch 2           # repeat every 2 s (delta log)
  python mem_probe.py --log probe.csv     # append a CSV row per sample
  python mem_probe.py --pid 1234          # explicit pid
  python mem_probe.py --base 0x7FF600000000
  python mem_probe.py --selftest          # synthetic target, no game needed
  python mem_probe.py --list-modules
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import struct
import subprocess
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

TARGET_PROCESS = "kards-Win64-Shipping.exe"
TARGET_MODULE = "kards-Win64-Shipping.exe"
TARGET_EXE_SIZE = 160489984
TARGET_EXE_MD5 = "395e470f06837f6e60ce5c53c6df2a22"
# toolhelp's modBaseSize is SizeOfImage (mapped image), not the file size
TARGET_IMAGE_SIZE = 0x9CC8000
KNOWN_IMAGES = {
    0x9CC8000: ("STEAM  ...\\common\\KARDS\\kards\\Binaries\\Win64", 160489984,
                "395e470f06837f6e60ce5c53c6df2a22"),
    0x9CBB000: ("launcher ...\\Games\\KARDS\\1.57.26586.launcher\\game", 160441344,
                "201773bc49f52ff8b9e6c8aae172ae03"),
    0x9CC4000: ("...\\Games\\KARDS\\default\\game", 160476160,
                "7c6a83c7d002d57d3581b87296eda98b"),
}

# ---------------------------------------------------------------- offsets ---
# Globals (Dumpspace/OffsetsInfo.json of the current build)
RVA_GWORLD = 0x08F625B0
RVA_GOBJECTS = 0x091FF4E0
RVA_GNAMES = 0x090E2E28

# UObject
OFF_UOBJECT_FLAGS = 0x08
OFF_UOBJECT_CLASS = 0x10
OFF_UOBJECT_NAME = 0x18
OFF_UOBJECT_OUTER = 0x20
OFF_UOBJECT_FNAME = 0x20

# UWorld
OFF_UWORLD_GAMESTATE = 0x1B0
SIZE_UWORLD = 2536

# Vtables (from the build's .idmap)
VFT = {
    "UObject_VFT": 0x07115348,
    "AActor_VFT": 0x070D3090,
    "AInfo_VFT": 0x07A54340,
    "AGameStateBase_VFT": 0x07A54610,
    "AGameState_VFT": 0x07952218,
    "AkardsGameState_VFT": 0x07E859E0,
    "UWorld_VFT": 0x075CB078,
    "APlayerController_VFT": 0x07B5F0F0,
}
GS_VFT_CANDIDATES = ["AkardsGameState_VFT", "AGameState_VFT", "AGameStateBase_VFT", "AInfo_VFT", "AActor_VFT"]

CLASS_SIZE_AKARDS_GAMESTATE = 912
CLASS_SIZE_BP_GAMESTATE_BATTLE = 1960

# AkardsGameState (base) - derived from the OLD build disassembly, anchor
# OnTamperingDetected @0x320 confirmed against the current build's reflection dump.
AGS = {
    "tamperFlag": (0x330, "u8"),
    "encryptionKey": (0x334, "i32"),
}
AGS_SIDE = {                      # 20-byte encrypted side values
    "kredit_left": 0x33C,
    "kredit_right": 0x350,
    "slot_left": 0x364,
    "slot_right": 0x378,
}

# ABP_GameState_Battle_C
BGS = {
    "FrontlineOwner": (0x3A0, "u8"),
    "HQ_DamagedThisTurn_Left": (0x4B0, "i32"),
    "HQ_DamagedThisTurn_Right": (0x4B4, "i32"),
    "KreditSlotsLost_Left": (0x4B8, "i32"),
    "KreditSlotsLost_Right": (0x4BC, "i32"),
    "hasCapturedFrontline": (0x4C0, "u8"),
    "ActionProcess": (0x531, "u8"),
    "FatigueDamageRight": (0x5D8, "i32"),
    "FatigueDamageLeft": (0x5DC, "i32"),
    "Left_HQ_CardID": (0x5E0, "i32"),
    "Left_HQ_Card_Ref": (0x5E8, "ptr"),
    "Right_HQ_CardID": (0x5F0, "i32"),
    "Right_HQ_Card_Ref": (0x5F8, "ptr"),
    "stopFurtherActions": (0x600, "u8"),
    "stopAttack": (0x601, "u8"),
    "startingSide": (0x668, "u8"),
    "LeftAllyFaction": (0x669, "u8"),
    "RightAllyFaction": (0x66A, "u8"),
    "LeftMainFaction": (0x66B, "u8"),
    "RightMainFaction": (0x66C, "u8"),
    "IsLocalClientTurn": (0x680, "u8"),
    "CurrentTurnNumberInBattle": (0x684, "i32"),
    "MatchFinished": (0x688, "u8"),
    "UnitDestroyedThisTurn": (0x689, "u8"),
    "hasPlayedWeatherCardThisTurn": (0x750, "u8"),
    "OperationKreditsSpentThisTurn": (0x754, "i32"),
}
BGS_ARRAYS = {
    "GameplayRestrictionEffects": 0x3F8,
    "DestroyedCardsIDs_All": 0x458,
    "DestroyedCardsIDs_Left": 0x468,
    "DestroyedCardsIDs_Right": 0x478,
    "DeckCardIDs_Right": 0x490,
    "DeckCardIDs_Left": 0x4A0,
    "unitsPlayedInFirstTurn": 0x4C8,
    "WaitPlayFromHandCards": 0x608,
    "AllStaticCardsSortedByName": 0x670,
    "AutoPlayerCardIDs": 0x6F0,
}
BGS_MAPS = {
    "cardsPlayedTurnMapped": 0x408,
    "elitesPlayed": 0x4E0,
    "AllCardsInBattle": 0x538,
    "CardFunctionTriggers": 0x588,
    "BuffsToRemoveEndOfTurn": 0x618,
    "DestroyedCardIDsByTurnNumber": 0x700,
    "ReconnectHiddenEnemyKreditsByTurnNumber": 0x758,
}
# TArray: {Data@0, ArrayNum@8, ArrayMax@0xC}; TSparseArray inside TSet/TMap
# starts with a TArray, so ArrayNum is also at +0x08 for a TMap.
OFF_TARRAY_NUM = 0x08

PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF


# ------------------------------------------------------------- process io ---
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256), ("szExePath", wt.WCHAR * 260),
    ]


k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
k32.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wt.BOOL
k32.CloseHandle.argtypes = [wt.HANDLE]


def enable_debug_privilege():
    """Best effort; only needed when the target runs elevated."""
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x20, 0x8
        SE_PRIVILEGE_ENABLED = 0x2

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        tok = wt.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(tok)):
            return False
        luid = LUID()
        if not adv.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        k32.CloseHandle(tok)
        return True
    except Exception:
        return False


def find_pid(exe_name):
    """Match on the process image name; tolerates a missing .exe suffix."""
    want = exe_name.lower()
    stem = want[:-4] if want.endswith(".exe") else want
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile.decode("mbcs", "replace").lower()
            if name == want or name == stem:
                return e.th32ProcessID
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return None


def all_pids(exe_name):
    want = exe_name.lower()
    stem = want[:-4] if want.endswith(".exe") else want
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile.decode("mbcs", "replace")
            if name.lower() in (want, stem):
                out.append(e.th32ProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def list_modules(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    try:
        m = MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = k32.Module32FirstW(snap, ctypes.byref(m))
        while ok:
            out.append((m.szModule, m.modBaseAddr or 0, m.modBaseSize, m.szExePath))
            ok = k32.Module32NextW(snap, ctypes.byref(m))
    finally:
        k32.CloseHandle(snap)
    return out


class Mem:
    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            self.h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise OSError("OpenProcess(%d) failed: err=%d" % (pid, ctypes.get_last_error()))

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            raise OSError("ReadProcessMemory(0x%X, %d) failed: err=%d" % (addr, size, ctypes.get_last_error()))
        return buf.raw[:got.value]

    def safe(self, addr, size):
        try:
            return self.read(addr, size)
        except OSError:
            return None

    def u8(self, a):
        d = self.safe(a, 1)
        return d[0] if d else None

    def u16(self, a):
        d = self.safe(a, 2)
        return struct.unpack("<H", d)[0] if d else None

    def u32(self, a):
        d = self.safe(a, 4)
        return struct.unpack("<I", d)[0] if d else None

    def i32(self, a):
        d = self.safe(a, 4)
        return struct.unpack("<i", d)[0] if d else None

    def u64(self, a):
        d = self.safe(a, 8)
        return struct.unpack("<Q", d)[0] if d else None

    def f32(self, a):
        d = self.safe(a, 4)
        return struct.unpack("<f", d)[0] if d else None

    def ptr(self, a):
        v = self.u64(a)
        if v is None:
            return None
        return v if PTR_MIN <= v <= PTR_MAX else None


def read_field(m, addr, kind):
    return {"u8": m.u8, "i32": m.i32, "ptr": m.ptr, "f32": m.f32}.get(kind, m.u32)(addr)


# ------------------------------------------------------------------ probe ---
def propsize_hits(m, uclass, wanted, lo=0x30, hi=0xC0):
    """Locate UStruct::PropertiesSize on a UClass by value (name-free identity)."""
    hits = []
    if not uclass:
        return hits
    for off in range(lo, hi, 4):
        v = m.u32(uclass + off)
        if v in wanted:
            hits.append((off, v))
    return hits


def locate(base, m):
    """GWorld -> UWorld -> GameState.

    Identity is established by UStruct::PropertiesSize (2536 for UWorld,
    912/1960 for the GameState).  The *_VFT addresses published in the .idmap
    do NOT match the live instance vptr, so they are reported but not trusted.
    """
    out = {"ok": False}

    gw_cell = base + RVA_GWORLD
    world = m.ptr(gw_cell)
    out["gworld_cell"] = gw_cell
    out["world"] = world
    if not world:
        out["error"] = "GWorld is null (world not created yet)"
        return out

    out["world_vptr"] = m.ptr(world)
    out["world_vptr_name"] = [n for n, r in VFT.items() if base + r == out["world_vptr"]]
    uclass = m.ptr(world + OFF_UOBJECT_CLASS)
    out["world_uclass"] = uclass
    hits = propsize_hits(m, uclass, (SIZE_UWORLD,))
    out["world_propsize_hits"] = hits
    out["world_is_uworld"] = bool(hits)
    if not hits:
        out["error"] = ("object at GWorld is not a UWorld: UClass=0x%X has no PropertiesSize==%d "
                        "in 0x30..0xC0 (vptr=0x%X)" % (uclass or 0, SIZE_UWORLD, out["world_vptr"] or 0))
        return out

    gs = m.ptr(world + OFF_UWORLD_GAMESTATE)
    out["gamestate"] = gs
    if not gs:
        out["error"] = "UWorld+0x1B0 (GameState) is null"
        return out

    gvt = m.ptr(gs)
    out["gamestate_vptr"] = gvt
    out["gamestate_vft_name"] = [n for n, r in VFT.items() if base + r == gvt]

    uclass = m.ptr(gs + OFF_UOBJECT_CLASS)
    out["gamestate_class"] = uclass
    props = propsize_hits(m, uclass, (CLASS_SIZE_AKARDS_GAMESTATE,
                                      CLASS_SIZE_BP_GAMESTATE_BATTLE, SIZE_UWORLD))
    out["class_size_hits"] = props
    if props:
        out["class_size"] = props[0][1]
        out["in_battle"] = props[0][1] == CLASS_SIZE_BP_GAMESTATE_BATTLE
    else:
        out["class_size"] = None
        out["in_battle"] = None

    out["ok"] = bool(out["world_is_uworld"] and gs)
    return out


SIDE_BLOCK_OFF = 0x330
SIDE_BLOCK_LEN = 0x60


def read_side_block(m, gs):
    """Read tamperFlag + encryptionKey + all four 20-byte side structs in ONE
    ReadProcessMemory.

    The game re-randomises den/base continuously, so reading the fields with
    separate RPM calls is a torn read that yields garbage.  The whole
    0x330..0x390 window must be captured atomically.

    value = ((enc ^ key) - base) / den     -- formula recovered from the OLD
    build's execgetKreditSlotBySide / execgetKreditBySide implementations and
    confirmed live: all four structs divide exactly.
    """
    buf = m.read(gs + SIDE_BLOCK_OFF, SIDE_BLOCK_LEN)
    tamper = buf[0]
    key = struct.unpack_from("<i", buf, 0x04)[0]
    sides = {}
    for name, off in AGS_SIDE.items():
        den, base_v, unk, enc, frame = struct.unpack_from("<5i", buf, off - SIDE_BLOCK_OFF)
        val = None
        if den:
            num = (enc ^ key) - base_v
            val = num // den if num % den == 0 else num / den
        sides[name] = {"den": den, "base": base_v, "unknown_8": unk,
                       "enc": enc, "frame": frame, "decrypted": val}
    return tamper, key, sides


def sample(base, m):
    loc = locate(base, m)
    rep = {"locate": loc, "t": time.time()}
    gs = loc.get("gamestate")
    if not gs:
        return rep

    tamper, key, sides = read_side_block(m, gs)
    rep["akards_gamestate"] = {"tamperFlag": tamper, "encryptionKey": key}
    rep["encrypted_sides"] = sides

    if loc.get("in_battle"):
        rep["battle"] = {k: read_field(m, gs + o, t) for k, (o, t) in BGS.items()}
        rep["arrays"] = {}
        for k, o in BGS_ARRAYS.items():
            p = m.ptr(gs + o)
            n = m.i32(gs + o + OFF_TARRAY_NUM)
            rep["arrays"][k] = {"ptr": p, "num": n}
        rep["maps"] = {}
        for k, o in BGS_MAPS.items():
            p = m.ptr(gs + o)
            n = m.i32(gs + o + OFF_TARRAY_NUM)
            rep["maps"][k] = {"ptr": p, "alloc": n}
    return rep


# --------------------------------------------------------------- printing ---
def print_report(rep, base, modinfo):
    loc = rep["locate"]
    print("=" * 78)
    print("module  : %s  base=0x%X  size=%d" % (modinfo[0], base, modinfo[1]))
    print("exe     : %s" % modinfo[2])
    print("-" * 78)
    if not loc.get("ok"):
        print("LOCATE FAILED: %s" % loc.get("error"))
        for k in ("gworld_cell", "world", "world_vptr", "world_vptr_name", "world_uclass",
                  "world_propsize_hits", "world_is_uworld", "gamestate", "gamestate_vptr",
                  "gamestate_vft_name", "class_size_hits"):
            if k in loc:
                v = loc[k]
                print("  %-22s %s" % (k, ("0x%X" % v) if isinstance(v, int) else v))
        print("=" * 78)
        return
    print("UWorld      : 0x%X   PropertiesSize hit %s   vptr=%s" % (
        loc["world"], loc["world_propsize_hits"], (loc.get("world_vptr_name") or ["unknown-VFT"])[0]))
    print("GameState   : 0x%X   vptr=%s" % (
        loc["gamestate"], (loc.get("gamestate_vft_name") or ["unknown-VFT"])[0]))
    print("class size  : %s  -> %s" % (
        loc["class_size"],
        "ABP_GameState_Battle_C (IN BATTLE)" if loc["in_battle"] else
        "AkardsGameState (not in battle)" if loc["class_size"] else "unidentified"))
    if loc["class_size_hits"]:
        print("size hits   : %s" % ", ".join("UClass+0x%X=%d" % h for h in loc["class_size_hits"]))
    gs = loc["gamestate"]

    print("-" * 78)
    print("AkardsGameState @0x%X" % gs)
    for k, v in rep["akards_gamestate"].items():
        off = AGS[k][0]
        print("  0x%03X  %-28s %s" % (off, k, v))
    print("  encrypted side values  (decrypted = ((enc ^ key) - base) / den)")
    for k, d in rep["encrypted_sides"].items():
        print("  0x%03X  %-28s den=%-6s base=%-8s enc=%-12s frame=%-6s -> %s" % (
            AGS_SIDE[k], k, d["den"], d["base"], d["enc"], d["frame"], d["decrypted"]))

    if "battle" in rep:
        print("-" * 78)
        print("ABP_GameState_Battle_C fields")
        for k, v in rep["battle"].items():
            off = BGS[k][0]
            extra = ""
            if BGS[k][1] == "ptr" and v:
                vt = None
                print_vt = None
                extra = "  obj"
            print("  0x%03X  %-42s %s%s" % (off, k, v, extra))
        print("  TArray / TSet counts (ArrayNum at +0x08, includes tombstones for TSet/TMap)")
        for k, v in rep["arrays"].items():
            print("  0x%03X  %-42s num=%-6s ptr=0x%X" % (BGS_ARRAYS[k], k, v["num"], v["ptr"] or 0))
        for k, v in rep["maps"].items():
            print("  0x%03X  %-42s alloc=%-6s ptr=0x%X" % (BGS_MAPS[k], k, v["alloc"], v["ptr"] or 0))
    print("=" * 78)


def csv_header():
    cols = ["t", "in_battle", "class_size", "turn", "local_turn", "slots_lost_L", "slots_lost_R",
            "kredit_L", "kredit_R", "slot_L", "slot_R", "op_spent", "frontline", "match_finished"]
    for name in AGS_SIDE:
        cols += [name + "_enc", name + "_dec"]
    return cols


def csv_row(rep):
    loc = rep["locate"]
    b = rep.get("battle", {})
    es = rep.get("encrypted_sides", {})
    dec = {k: v.get("decrypted") for k, v in es.items()}
    row = [rep["t"], loc.get("in_battle"), loc.get("class_size"),
           b.get("CurrentTurnNumberInBattle"), b.get("IsLocalClientTurn"),
           b.get("KreditSlotsLost_Left"), b.get("KreditSlotsLost_Right"),
           dec.get("kredit_left"), dec.get("kredit_right"),
           dec.get("slot_left"), dec.get("slot_right"),
           b.get("OperationKreditsSpentThisTurn"), b.get("FrontlineOwner"), b.get("MatchFinished")]
    for name in AGS_SIDE:
        row += [es.get(name, {}).get("enc"), es.get(name, {}).get("decrypted")]
    return row


# --------------------------------------------------------------- selftest ---
FAKE_TARGET = r'''
import ctypes, json, sys, time
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
k32.VirtualAlloc.restype = ctypes.c_void_p
MEM_RESERVE, MEM_COMMIT = 0x2000, 0x1000
PAGE_READWRITE = 0x04
RESERVE = 0x0A000000
base = k32.VirtualAlloc(None, RESERVE, MEM_RESERVE, PAGE_READWRITE)
assert base, "reserve failed"

def commit(addr, size=0x1000):
    a = addr & ~0xFFF
    r = k32.VirtualAlloc(ctypes.c_void_p(a), size + (addr - a), MEM_COMMIT, PAGE_READWRITE)
    assert r, "commit 0x%X failed" % addr
    return r

def blob(size):
    b = ctypes.create_string_buffer(size)
    return b, ctypes.addressof(b)

def w64(addr, v): ctypes.c_uint64.from_address(addr).value = v
def w32(addr, v): ctypes.c_uint32.from_address(addr).value = v
def w8(addr, v):  ctypes.c_uint8.from_address(addr).value = v

RVA_GWORLD, RVA_UWORLD_VFT, RVA_GS_VFT = 0x08F625B0, 0x075CB078, 0x07E859E0
OFF_GAMESTATE = 0x1B0

# fake vtables
commit(base + RVA_UWORLD_VFT); commit(base + RVA_GS_VFT)
world = base + 0x05000000
gs = base + 0x06000000
uclass = base + 0x07000000
commit(world + OFF_GAMESTATE); commit(gs); commit(uclass)

w64(world, base + RVA_UWORLD_VFT)
w64(world + OFF_GAMESTATE, gs)
w64(gs, base + RVA_GS_VFT)
w64(gs + 0x10, uclass)
w32(uclass + 0x50, 1960)          # UStruct::PropertiesSize
w8(gs + 0x330, 0)                 # tamperFlag
w32(gs + 0x334, 0x5A5A5A5A)       # key
# kredit_left @0x33C: want 7  -> den=1 base=0 enc = 7 ^ key
w32(gs + 0x33C + 0x00, 1)
w32(gs + 0x33C + 0x04, 0)
w32(gs + 0x33C + 0x08, 0)
w32(gs + 0x33C + 0x0C, 7 ^ 0x5A5A5A5A)
w32(gs + 0x33C + 0x10, 3)
# slot_left @0x364: want 5
w32(gs + 0x364 + 0x00, 1)
w32(gs + 0x364 + 0x0C, 5 ^ 0x5A5A5A5A)
# battle fields
w32(gs + 0x4B8, 2); w32(gs + 0x4BC, 0); w32(gs + 0x684, 9); w8(gs + 0x680, 1)
w8(gs + 0x3A0, 1); w32(gs + 0x754, 6); w8(gs + 0x688, 0)
commit(base + RVA_GWORLD); w64(base + RVA_GWORLD, world)
# fake arrays/maps: ArrayNum at +0x08
commit(gs + 0x538); w32(gs + 0x538 + 0x08, 13)
commit(gs + 0x458); w32(gs + 0x458 + 0x08, 4)

print(json.dumps({"base": base, "world": world, "gs": gs, "uclass": uclass}))
sys.stdout.flush()
time.sleep(120)
'''


def run_selftest(argv):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".py", prefix="kards_faketarget_")
    with os.fdopen(fd, "w") as f:
        f.write(FAKE_TARGET)
    p = subprocess.Popen([sys.executable, path], stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline()
    try:
        info = json.loads(line)
    except Exception:
        print("selftest target did not report; raw: %r" % line)
        p.kill()
        return 2
    print("selftest target pid=%d base=0x%X" % (p.pid, info["base"]))
    m = Mem(p.pid)
    rep = sample(info["base"], m)
    print_report(rep, info["base"], ("<fake>", 0x0A000000, "synthetic target"))

    ok = True
    checks = []

    def chk(name, got, want):
        nonlocal ok
        good = (got == want)
        ok = ok and good
        checks.append((name, got, want, good))

    chk("locate ok", rep["locate"].get("ok"), True)
    chk("class size", rep["locate"].get("class_size"), 1960)
    chk("in battle", rep["locate"].get("in_battle"), True)
    chk("kredit_left decrypted", rep["encrypted_sides"]["kredit_left"]["decrypted"], 7)
    chk("slot_left decrypted", rep["encrypted_sides"]["slot_left"]["decrypted"], 5)
    chk("turn", rep["battle"]["CurrentTurnNumberInBattle"], 9)
    chk("local turn", rep["battle"]["IsLocalClientTurn"], 1)
    chk("slots lost L", rep["battle"]["KreditSlotsLost_Left"], 2)
    chk("op spent", rep["battle"]["OperationKreditsSpentThisTurn"], 6)
    chk("AllCardsInBattle alloc", rep["maps"]["AllCardsInBattle"]["alloc"], 13)
    chk("DestroyedCardsIDs_All num", rep["arrays"]["DestroyedCardsIDs_All"]["num"], 4)

    print("SELFTEST")
    for name, got, want, good in checks:
        print("  [%s] %-28s got=%s want=%s" % ("PASS" if good else "FAIL", name, got, want))
    print("SELFTEST RESULT: %s" % ("PASS" if ok else "FAIL"))
    m.close()
    p.kill()
    try:
        os.unlink(path)
    except OSError:
        pass
    return 0 if ok else 1


# ------------------------------------------------------------------ main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--base", type=lambda s: int(s, 0))
    ap.add_argument("--watch", type=float, default=0.0, help="seconds between samples")
    ap.add_argument("--log", help="append CSV rows here")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list-modules", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip build validation")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest(sys.argv)

    if not enable_debug_privilege():
        pass  # not fatal; same-user targets work without it

    pid = args.pid or find_pid(TARGET_PROCESS)
    if not pid:
        print("ERROR: %s is not running" % TARGET_PROCESS)
        return 2

    mods = list_modules(pid)
    if args.list_modules:
        for name, b, size, path in sorted(mods, key=lambda x: x[1]):
            print("0x%016X  %10d  %-28s %s" % (b, size, name, path))
        return 0

    mod = None
    for name, b, size, path in mods:
        if name.lower() == TARGET_MODULE.lower():
            mod = (name, b, size, path)
            break
    if mod is None:
        print("ERROR: module %s not found in pid %d" % (TARGET_MODULE, pid))
        return 2

    base = args.base or mod[1]
    print("pid=%d  module=%s  base=0x%X  size=%d" % (pid, mod[0], base, mod[2]))

    if not args.force:
        problems = []
        if mod[2] != TARGET_IMAGE_SIZE:
            who = KNOWN_IMAGES.get(mod[2])
            if who:
                problems.append("module SizeOfImage 0x%X is the %s build (%d bytes, md5 %s), "
                                "NOT the Steam build the offsets were verified against" % (mod[2], who[0], who[1], who[2]))
            else:
                problems.append("module SizeOfImage 0x%X != expected 0x%X (unknown build)"
                                % (mod[2], TARGET_IMAGE_SIZE))
        disk = None
        if mod[3] and os.path.exists(mod[3]):
            h = hashlib.md5()
            with open(mod[3], "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            disk = h.hexdigest()
            if disk != TARGET_EXE_MD5:
                problems.append("on-disk md5 %s != expected %s" % (disk, TARGET_EXE_MD5))
        if problems:
            print("BUILD MISMATCH - refusing to read (offsets would be wrong):")
            for p in problems:
                print("   %s" % p)
            print("   pass --force to override")
            return 3

    m = Mem(pid)
    try:
        if args.watch <= 0:
            rep = sample(base, m)
            if args.json:
                print(json.dumps(rep, indent=2, default=str))
            else:
                print_report(rep, base, (mod[0], mod[2], mod[3]))
            return 0 if rep["locate"].get("ok") else 1

        cols = csv_header()
        fh = None
        new = not args.log or not os.path.exists(args.log)
        if args.log:
            fh = open(args.log, "a", encoding="utf-8")
            if new:
                fh.write(",".join(cols) + "\n")
        print(",".join(cols))
        try:
            while True:
                rep = sample(base, m)
                row = csv_row(rep)
                line = ",".join("" if v is None else str(v) for v in row)
                print(line)
                if fh:
                    fh.write(line + "\n")
                    fh.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            if fh:
                fh.close()
    finally:
        m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
