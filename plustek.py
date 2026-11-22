"""Plustek OpticFilm 8200i capture through the vendor ScanAPI driver (Scndrvu_x64.drv).

Reverse-engineered interface; see NOTES.md. All driver calls return 0 on success.
"""
import ctypes as C
import os
import struct
import threading
import time

import numpy as np

DRV_DIR = r"C:\Program Files (x86)\Plustek\OpticFilm 8200i"
DRV_NAME = "Scndrvu_x64.drv"

SOURCE_FILM = 1
MODE_GRAY, MODE_COLOR = 1, 2
UNIT_PIXEL = 3
FLAG_IR = 1 << 16          # scan has an infrared page (Read returns 6 at end of the colour page)
FLAG_IR_PAGE = 1 << 28     # this StartScan reads the infrared page
RET_OK, RET_EOF, RET_NEXT_PAGE = 0, 2, 6
MAX_W_IN, MAX_H_IN = 1.44, 1.0      # source 1 scan window, inches

ERRORS = {-90: "scanner not found on USB (off, asleep or held by another program)",
          -89: "driver general error", -94: "not registered / bad state", -86: "scanner busy (lock timeout)"}


EXPOSURE_MIN_DPI = 900      # the driver rejects exposure levels below this resolution
CLIP_START, CLIP_END = 52000.0, 61000.0


def merge_exposures(lo, hi):
    """Combine a normal pass and a long-exposure pass of the same frame (multi-exposure).

    The long pass has far less noise where the film is dense, but clips where it is clear. Each pixel
    takes the long pass (scaled back to normal units) unless that is near clipping, then the normal one.
    The real gain is measured per channel from pixels that are well exposed in both. Returns
    (merged uint16 in normal-pass units, gain per channel)."""
    lo_s, hi_s = lo[::7, ::7].reshape(-1, lo.shape[2]).astype(np.float32), hi[::7, ::7].reshape(-1, hi.shape[2]).astype(np.float32)
    gains = []
    for c in range(lo.shape[2]):
        ok = (lo_s[:, c] > 1500) & (hi_s[:, c] < CLIP_START)
        gains.append(float(np.median(hi_s[ok, c] / lo_s[ok, c])) if ok.sum() > 200 else 0.0)
    out = np.empty(lo.shape, np.uint16)
    for c in range(lo.shape[2]):
        if gains[c] < 1.2:                                   # nothing usable from the long pass in this channel
            out[..., c] = lo[..., c]
            continue
        h = hi[..., c].astype(np.float32)
        w = np.clip((CLIP_END - h) / (CLIP_END - CLIP_START), 0.0, 1.0)
        m = w * (h / gains[c]) + (1.0 - w) * lo[..., c]
        out[..., c] = np.clip(m + 0.5, 0, 65535).astype(np.uint16)
    return out, gains


class ScannerError(RuntimeError):
    def __init__(self, where, code):
        super().__init__(f"{where} failed: {code} ({ERRORS.get(code, 'unknown')})")
        self.code = code


def redirect_import(module_base, dll, name, target):
    """Point one imported function of a loaded 64-bit module at `target` (an address). Returns True if found.

    Only that module's own import table is changed; the rest of the process keeps the real function."""
    def u32(addr):
        return C.c_uint32.from_address(addr).value

    pe = module_base + u32(module_base + 0x3C)
    opt = pe + 24
    if C.c_uint16.from_address(opt).value != 0x20B:           # PE32+ only
        return False
    desc = module_base + u32(opt + 112 + 8)                    # data directory 1: imports
    if desc == module_base:
        return False
    while u32(desc + 12):
        if C.string_at(module_base + u32(desc + 12)).lower() == dll.lower():
            names = module_base + (u32(desc) or u32(desc + 16))
            slots = module_base + u32(desc + 16)
            i = 0
            while True:
                entry = C.c_uint64.from_address(names + 8 * i).value
                if not entry:
                    break
                if not entry >> 63 and C.string_at(module_base + entry + 2) == name:
                    slot, old = slots + 8 * i, C.c_uint32()
                    k32 = C.WinDLL("kernel32", use_last_error=True)
                    k32.VirtualProtect.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32, C.c_void_p]
                    if not k32.VirtualProtect(slot, 8, 0x04, C.byref(old)):
                        return False
                    C.c_uint64.from_address(slot).value = target
                    k32.VirtualProtect(slot, 8, old.value, C.byref(old))
                    return True
                i += 1
        desc += 20
    return False


class Scanner:
    """One open connection to the scanner. Not thread-safe: call from one thread at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._registered = False
        cwd = os.getcwd()
        os.add_dll_directory(DRV_DIR)
        os.chdir(DRV_DIR)            # driver looks for its ini files in the current directory
        try:
            self._drv = C.WinDLL(os.path.join(DRV_DIR, DRV_NAME))
        finally:
            os.chdir(cwd)
        # The driver reports some failures (a sleeping scanner, for one) with its own debug message box, such as
        # "SCANAPI_RET_GENERAL_ERROR 14!", which blocks the call until someone clicks it. Its message boxes are
        # answered here instead and the text kept; the failure still comes back as an error code.
        self.messages = []

        def _on_message_box(hwnd, text, caption, kind):
            self.messages.append((text or b"").decode("mbcs", "replace"))
            del self.messages[:-20]
            return 1                                           # IDOK

        self._msgbox_cb = C.WINFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint)(_on_message_box)
        try:
            self.quiet = redirect_import(self._drv._handle, b"user32.dll", b"MessageBoxA",
                                         C.cast(self._msgbox_cb, C.c_void_p).value)
        except Exception:
            self.quiet = False
        self._drv.SCANAPI_GetFunctionTable.restype = C.c_void_p
        slots = (C.c_void_p * 46).from_address(self._drv.SCANAPI_GetFunctionTable())

        def fn(i, *args):
            return C.CFUNCTYPE(C.c_int, *args)(slots[i])

        self._register = fn(0, C.c_void_p)
        self._unregister = fn(1)
        self._layout = fn(5, C.c_void_p, C.c_void_p)
        self._start = fn(6, C.c_void_p)
        self._read = fn(7, C.c_void_p, C.c_uint32, C.c_void_p)
        self._stop = fn(8)
        self._end = fn(39, C.c_int)
        self._status = fn(4, C.c_int, C.c_void_p)
        self._lamp_time = fn(20, C.c_void_p, C.c_int, C.c_int)     # (int *minutes, int set, int type); type 1 = standby
        self.standby_was = None
        self.events = []

        def _on_event(code, value):
            self.events.append((int(code), int(value)))
            return 0

        self._event_cb = C.CFUNCTYPE(C.c_int, C.c_int, C.c_int)(_on_event)     # kept alive for the driver

    # -- connection ---------------------------------------------------------
    def open(self):
        cwd = os.getcwd()
        os.chdir(DRV_DIR)
        try:
            r = self._register((C.c_uint16 * 4)(1, 0, 0, 4))
        finally:
            os.chdir(cwd)
        if r:
            raise ScannerError("Register", r)
        self._registered = True
        self.disable_standby()
        return self

    def power_times(self):
        """(lamp-off minutes, standby minutes) as the driver reports them; None where a query fails."""
        out = []
        for typ in (0, 1):
            v = C.c_int(-1)
            out.append(v.value if self._lamp_time(C.byref(v), 0, typ) == 0 else None)
        return tuple(out)

    def disable_standby(self):
        """Stop the scanner putting itself to sleep while idle.

        Plustek's driver gives the scanner two idle timers: "standby" (10 min as found) and "lamp off"
        (the scanner's own default is 5 min, and for this LED scanner lamp-off means it drops off USB
        until its button is pressed). Standby is set to 0 (driver leaves it off). Lamp-off cannot be
        switched off, so it is set to the maximum the driver accepts, 60 minutes; calling this again
        re-arms it, which the app does every few minutes while it is open."""
        if not self._registered or not self._lock.acquire(blocking=False):
            return None
        try:
            before = self.power_times()
            if self.standby_was is None:
                self.standby_was = before
            self._lamp_time(C.byref(C.c_int(0)), 1, 1)
            self._lamp_time(C.byref(C.c_int(60)), 1, 0)
            return before, self.power_times()
        except OSError:
            return None
        finally:
            self._lock.release()

    def close(self):
        if self._registered:
            self._unregister()
            self._registered = False

    __enter__ = open

    def __exit__(self, *exc):
        self.close()

    def ping(self):
        """Cheap status query; also keeps the USB link from idling out. True if the scanner answers."""
        if not self._registered or not self._lock.acquire(blocking=False):
            return self._registered
        try:
            st = C.create_string_buffer(64)
            struct.pack_into("<H", st, 0, 1)
            return self._status(SOURCE_FILM, st) == 0
        finally:
            self._lock.release()

    # -- scanning -----------------------------------------------------------
    def scan(self, dpi=600, x=0, y=0, w=None, h=None, infrared=False, exposure=0, samples=1, multi_exposure=0, flags=0,
             param_patch=None, progress=None, cancel=None):
        """Scan a window given in pixels at `dpi`. Returns dict(rgb=uint16 HxWx3, ir=uint16 HxW or None).

        Data is linear, unprocessed sensor output. `progress(fraction)` is called while reading;
        `cancel()` returning True aborts.
        """
        full_w, full_h = int(MAX_W_IN * dpi), int(MAX_H_IN * dpi)
        w = full_w - x if w is None else w
        h = full_h - y if h is None else h
        prop = C.create_string_buffer(0x3f)
        struct.pack_into("<iiiHII4dI", prop, 0, SOURCE_FILM, 0, MODE_COLOR, 16, dpi, dpi,
                         float(x), float(y), float(w), float(h), UNIT_PIXEL)

        def make_param(fl, exposure=exposure):
            param = C.create_string_buffer(0x51)
            struct.pack_into("<QI", param, 0, C.addressof(prop), fl)
            # Event callback (calibration begin/end ...). VueScan always supplies one, and the driver
            # needs it when an exposure level is set: without it the scan thread dies and Read fails.
            struct.pack_into("<Q", param, 0x14, C.cast(self._event_cb, C.c_void_p).value)
            if exposure and int(exposure) > 1:
                struct.pack_into("<I", param, 0x3b, 1)          # bit0: use explicit exposure level
                struct.pack_into("<B", param, 0x3f, int(exposure))
            for off, fmt, val in (param_patch or []):
                struct.pack_into(fmt, param, off, val)
            return param

        samples = max(1, int(samples))             # colour pass repeated and averaged to cut sensor noise
        colour_flags = flags | (FLAG_IR if infrared else 0)
        passes = [(colour_flags, exposure)] * samples
        long_level = int(multi_exposure) if multi_exposure and dpi >= EXPOSURE_MIN_DPI else 0
        if long_level > 1:
            passes.append((colour_flags, long_level))       # multi-exposure: one extra, longer colour pass
        if infrared:
            passes.append((flags | FLAG_IR | FLAG_IR_PAGE, 0))
        pages, info = [], dict(rets=[], times=[])
        with self._lock:
            lay = C.create_string_buffer(64)
            r = self._layout(prop, lay)
            if r:
                raise ScannerError("GetImageLayout", r)
            ppl, bpl, lines = struct.unpack_from("<3I", lay)
            info.update(ppl=ppl, bpl=bpl, lines=lines)
            try:
                for i, (fl, level) in enumerate(passes):
                    if cancel and cancel():
                        info["cancelled"] = True
                        break
                    t0 = time.time()
                    param = make_param(fl, level)
                    r = self._start(param)
                    if r:
                        raise ScannerError("StartScan", r)
                    t1 = time.time()
                    try:
                        buf = self._read_page(bpl, lines, info,
                                              (lambda f, p=i: progress((p + f) / len(passes))) if progress else None,
                                              cancel)
                    finally:
                        self._stop()
                    info["times"].append((round(t1 - t0, 1), round(time.time() - t1, 1)))
                    pages.append(buf)
                    if len(buf) < bpl * lines:
                        break
            finally:
                self._end(0)
        out = dict(rgb=None, ir=None, info=info)
        full = [p for p in pages if len(p) >= bpl * lines]
        complete = len(full) == len(passes)
        long_page = None
        if infrared and complete:
            out["ir"] = np.frombuffer(bytes(full.pop()), "<u2").reshape(lines, ppl, -1)[..., 0]
        if long_level > 1 and complete:
            long_page = np.frombuffer(bytes(full.pop()), "<u2").reshape(lines, ppl, -1)
        colour = full[:samples]
        if len(colour) == 1:
            out["rgb"] = np.frombuffer(bytes(colour[0]), "<u2").reshape(lines, ppl, -1)
        elif colour:
            acc = np.zeros((lines, ppl, bpl // ppl // 2), np.uint32)
            for p in colour:
                acc += np.frombuffer(bytes(p), "<u2").reshape(lines, ppl, -1)
            out["rgb"] = ((acc + len(colour) // 2) // len(colour)).astype(np.uint16)
        elif pages and len(pages[0]) >= bpl:               # cancelled or cut short: return what arrived
            n = len(pages[0]) // bpl
            out["rgb"] = np.frombuffer(bytes(pages[0][:n * bpl]), "<u2").reshape(n, ppl, -1)
        info["samples"] = len(colour)
        if long_page is not None and out["rgb"] is not None:
            out["rgb"], info["exposure_gain"] = merge_exposures(out["rgb"], long_page)
        return out

    def _read_page(self, bpl, lines, info, progress, cancel):
        """Read one page (pass). apiRead takes a byte count and returns a LINE count."""
        total = bpl * lines
        step = max(1, min(64, (4 << 20) // bpl))
        chunk = C.create_string_buffer(bpl * step)
        got = C.c_uint32()
        buf = bytearray()
        idle_since = time.time()
        while len(buf) < total:
            if cancel and cancel():
                break
            want = min(step, (total - len(buf)) // bpl)
            r = self._read(chunk, want * bpl, C.byref(got))
            if got.value:
                buf += chunk.raw[:got.value * bpl]
                idle_since = time.time()
                if progress:
                    progress(len(buf) / total)
            if r == RET_NEXT_PAGE:
                info["rets"].append((len(buf), r))
                break
            if r not in (RET_OK, RET_EOF):
                info["rets"].append((len(buf), r))
                raise ScannerError("Read", r)
            if not got.value:
                if time.time() - idle_since > 60:
                    info["rets"].append((len(buf), "timeout"))
                    break
                time.sleep(0.02)
        return buf
