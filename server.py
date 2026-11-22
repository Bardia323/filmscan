"""Film scanning app: local web UI on top of plustek.py (capture) and film.py (colour).

Run:  .venv\\Scripts\\python server.py     then open http://127.0.0.1:8765
"""
import datetime
import io
import json
import os
import threading
import time
import traceback
import webbrowser

import cv2
import numpy as np
import tifffile
from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image

import colorspace
import film
from plustek import Scanner, ScannerError

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "settings.json")
PRESET_FILE = os.path.join(HERE, "presets.json")
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "Pictures"))
PORT = 8765

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")


BASE_HISTORY = 12


def same_base(a, b):
    return all(abs(x - y) <= 0.0005 * max(x, y, 1.0) for x, y in zip(a, b))


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = film.Settings()
        self.base_locked = False          # film base held for the roll
        self.auto_exposure = True         # set exposure automatically on each new preview
        self.out_dir = DEFAULT_OUT
        self.scan_dpi = 3600
        self.preview_dpi = 600
        self.save_raw = False             # also keep the untouched sensor data next to each scan
        self.jpeg_quality = 95
        self.samples = 1                  # colour passes averaged per scan
        self.multi_exposure = 0           # 0 = off, 2 or 4 = extra long-exposure pass merged in
        self.colorspace = "srgb"          # srgb | adobe | prophoto | icc:<file name of an installed profile>
        self.window = None                # native window, for file dialogs
        self.cancel = False               # set to stop the running scan
        self.infrared = True
        self.fmt = "tiff16"
        self.preview_rgb = None           # raw, PREVIEW size
        self.preview_ir = None
        self.full_rgb = None              # last full-resolution raw scan (kept for re-processing)
        self.full_ir = None
        self.full_dpi = None
        self.last_file = None
        self.last_crop = None
        self.last_version = None
        self.art_crop = (0.0, 0.0, 1.0, 1.0)   # second crop, on the finished scan
        self.base_history = []            # film bases used before the current one, newest first
        self._seen_base = None
        self.level_match = None           # per-channel gain applied to the last scan to match its preview
        self.version = 0                  # bumps whenever the picture would look different
        self.scanner = None
        self.scanner_ok = False
        self.scanner_msg = "not connected"
        self.job = dict(id=0, kind=None, state="idle", progress=0.0, message="", error=None)
        self.load()

    def load(self):
        try:
            d = json.load(open(STATE_FILE))
        except Exception:
            return
        self.settings = film.Settings.from_dict(d.get("settings", {}))
        self.base_history = [tuple(float(v) for v in b) for b in d.get("base_history", []) if len(b) == 3][:BASE_HISTORY]
        for k in ("base_locked", "auto_exposure", "out_dir", "scan_dpi", "preview_dpi", "infrared", "fmt", "save_raw", "jpeg_quality", "samples", "colorspace", "multi_exposure"):
            if k in d:
                setattr(self, k, d[k])

    def save(self):
        d = dict(settings=self.settings.to_dict(), base_locked=self.base_locked, auto_exposure=self.auto_exposure,
                 out_dir=self.out_dir, scan_dpi=self.scan_dpi, preview_dpi=self.preview_dpi, infrared=self.infrared,
                 fmt=self.fmt, save_raw=self.save_raw, jpeg_quality=self.jpeg_quality,
                 samples=self.samples, colorspace=self.colorspace, multi_exposure=self.multi_exposure,
                 base_history=[list(b) for b in self.base_history])
        tmp = STATE_FILE + ".tmp"
        json.dump(d, open(tmp, "w"), indent=1)
        os.replace(tmp, STATE_FILE)

    def src_size(self):
        """(width, height) in pixels of the picture on screen."""
        if self.full_rgb is not None:                  # a scan is stored already rotated and cut to the film crop
            h, w = self.full_rgb.shape[:2]
            return [w, h]
        if self.preview_rgb is None:
            return None
        h, w = self.preview_rgb.shape[:2]
        return [h, w] if self.settings.rotate % 2 else [w, h]

    def shown_settings(self):
        """Settings as the UI should see them: while a scan is on screen the crop box is the artistic crop."""
        d = self.settings.to_dict()
        if self.full_rgb is not None:
            d["crop"] = list(self.art_crop)
        return d

    def note_base(self):
        """Keep the film bases that get replaced, however that happens (pick, measure, preset, undo), so a base
        is never lost. Caller holds the lock."""
        cur = tuple(float(v) for v in self.settings.base)
        old = self._seen_base
        self._seen_base = cur
        if old is None or same_base(old, cur):
            return
        self.base_history = ([old] + [b for b in self.base_history
                                      if not same_base(b, old) and not same_base(b, cur)])[:BASE_HISTORY]

    def public(self):
        with self.lock:
            self.note_base()
            return dict(settings=self.shown_settings(), base_locked=self.base_locked,
                        auto_exposure=self.auto_exposure, out_dir=self.out_dir, scan_dpi=self.scan_dpi,
                        infrared=self.infrared, fmt=self.fmt, preview_dpi=self.preview_dpi, save_raw=self.save_raw,
                        jpeg_quality=self.jpeg_quality, samples=self.samples, colorspace=self.colorspace,
                        multi_exposure=self.multi_exposure,
                        colorspaces=[dict(id=e["id"], name=e["name"], kind=e["kind"]) for e in colorspace.installed_profiles()],
                        has_preview=self.preview_rgb is not None, src_size=self.src_size(),
                        viewing="scan" if self.full_rgb is not None else "preview",
                        has_full=self.full_rgb is not None, full_dpi=self.full_dpi, last_file=self.last_file,
                        last_crop=self.last_crop, crop_on_scan=True, last_version=self.last_version, versions=True,
                        base_history=[list(b) for b in self.base_history], level_match=self.level_match,
                        version=self.version, scanner_ok=self.scanner_ok, scanner_msg=self.scanner_msg,
                        job=dict(self.job))


S = State()


# -- scanner connection ---------------------------------------------------------

def get_scanner():
    """Open the scanner if needed. Raises ScannerError with a readable message."""
    with S.lock:
        if S.scanner is not None and S.scanner_ok:
            return S.scanner
        try:
            if S.scanner is None:
                S.scanner = Scanner()
            else:
                S.scanner.close()
            S.scanner.open()
            S.scanner_ok, S.scanner_msg = True, "connected"
        except ScannerError as e:
            S.scanner_ok, S.scanner_msg = False, str(e)
            raise
        except OSError as e:
            S.scanner_ok, S.scanner_msg = False, f"driver could not be loaded: {e}"
            raise ScannerError("LoadDriver", -1)
        return S.scanner


def keepalive():
    """Ping the scanner while idle so the USB link does not time out; notice when it goes away."""
    last_awake = 0.0
    while True:
        time.sleep(45)
        try:
            with S.lock:
                sc, busy = S.scanner, S.job["state"] == "running"
            if busy:
                continue
            if sc is None or not S.scanner_ok:
                try:
                    get_scanner()
                except Exception:
                    pass
                continue
            if not sc.ping():
                with S.lock:
                    S.scanner_ok, S.scanner_msg = False, "scanner stopped answering (press its power button)"
            elif time.time() - last_awake > 180:
                got = sc.disable_standby()               # re-arm the scanner's idle timer so it never runs out
                if got:
                    last_awake = time.time()
                    with open(os.path.join(HERE, "power.log"), "a", encoding="utf-8") as f:
                        f.write(time.strftime('%H:%M:%S') + f" lamp/standby before {got[0]} after {got[1]}" + chr(10))
        except Exception:
            traceback.print_exc()


# -- jobs -----------------------------------------------------------------------

class Cancelled(Exception):
    pass


def cancelled():
    return S.cancel


def start_job(kind, fn):
    with S.lock:
        if S.job["state"] == "running":
            return False
        S.job = dict(id=S.job["id"] + 1, kind=kind, state="running", progress=0.0, message="starting", error=None)
        S.cancel = False

    def run():
        try:
            try:
                fn()
            except ScannerError:
                if kind not in ("preview", "scan") or S.cancel:
                    raise
                # The link may only be stale (the scanner slept and was woken): connect again and try once more.
                with S.lock:
                    S.scanner_ok = False
                set_job(0.0, "reconnecting to the scanner")
                time.sleep(1.0)
                fn()
            with S.lock:
                S.job.update(state="done", progress=1.0)
        except Cancelled:
            with S.lock:
                S.job.update(state="cancelled", message="Cancelled. Nothing was saved.")
        except Exception as e:
            traceback.print_exc()
            with S.lock:
                S.job.update(state="error", error=str(e))
                if isinstance(e, ScannerError):
                    S.scanner_ok, S.scanner_msg = False, str(e)
                    S.job["error"] = ("The scanner is not answering. It is probably asleep: press its power button, "
                                      "wait until the light is steady, then try again. (" + str(e) + ")")

    threading.Thread(target=run, daemon=True).start()
    return True


def set_job(progress=None, message=None):
    with S.lock:
        if progress is not None:
            S.job["progress"] = float(progress)
        if message is not None:
            S.job["message"] = message


def job_preview():
    sc = get_scanner()
    set_job(0.02, "calibrating")
    r = sc.scan(dpi=S.preview_dpi, infrared=False, cancel=cancelled,
                progress=lambda f: set_job(0.5 + 0.5 * f, "reading preview"))
    if S.cancel:
        raise Cancelled()
    if r["rgb"] is None:
        raise RuntimeError("scanner returned no data")
    with S.lock:
        S.preview_rgb, S.preview_ir = r["rgb"], None
        S.full_rgb = S.full_ir = S.full_dpi = None
        after_new_frame()
        S.version += 1
        S.save()
    set_job(1.0, "preview ready")


def active_settings():
    """Settings for the picture currently in memory. Caller holds S.lock.

    Preview: the settings as they are (crop = film crop). Scan: it is stored already oriented and cut to the film
    crop, so geometry is neutral, there is no holder to look for, and the crop is the artistic one."""
    s = film.Settings.from_dict(S.settings.to_dict())
    if S.full_rgb is not None:
        s.flip_h = s.flip_v = False
        s.rotate, s.angle, s.holder = 0, 0.0, False
        s.crop = tuple(S.art_crop)
    return s


def whole(s):
    s = film.Settings.from_dict(s.to_dict())
    s.crop = (0.0, 0.0, 1.0, 1.0)
    return s


def bake(rgb, ir, s):
    """Sensor data -> the scan: oriented and cut to the film crop."""
    out = np.ascontiguousarray(film.crop_frac(film.orient(rgb, s), s.crop))
    out_ir = None if ir is None else np.ascontiguousarray(film.crop_frac(film.orient(ir, s), s.crop))
    return out, out_ir


def small_copy(a, dpi):
    """Reduced copy used for measuring (film base, exposure, picks)."""
    if a is None:
        return None
    f = min(1.0, float(S.preview_dpi) / dpi)
    if f >= 1.0:
        return a
    return cv2.resize(a, (max(8, int(a.shape[1] * f)), max(8, int(a.shape[0] * f))), interpolation=cv2.INTER_AREA)


def level_gain(scan, preview):
    """Per-channel gain that brings a scan to the levels of the preview of the same frame, or None.

    The scanner calibrates before every pass and does not land on exactly the same levels each time, least of all
    at a different resolution. On thin negatives, where the picture sits close to the film base, a difference of
    one percent is a visible colour shift. The settings were chosen looking at the preview, so the scan follows it.
    Returns None when the two do not look like the same frame."""
    ph, pw = preview.shape[:2]
    if abs(scan.shape[1] / scan.shape[0] - pw / ph) > 0.02:
        return None
    f = max(1, scan.shape[1] // 1600)                        # thin the scan first so the resize stays cheap
    small = cv2.resize(scan[::f, ::f].astype(np.float32), (pw, ph), interpolation=cv2.INTER_AREA)
    sigma = max(2.0, pw / 200.0)
    win = (slice(ph // 6, ph - ph // 6), slice(pw // 6, pw - pw // 6))
    a = cv2.GaussianBlur(small, (0, 0), sigma)[win].reshape(-1, 3)
    b = cv2.GaussianBlur(preview.astype(np.float32), (0, 0), sigma)[win].reshape(-1, 3)
    ok = (a.min(axis=1) > 1500) & (b.min(axis=1) > 1500) & (a.max(axis=1) < 60000) & (b.max(axis=1) < 60000)
    if ok.sum() < 2000:
        return None
    ratio = b[ok] / a[ok]
    gain = np.median(ratio, axis=0)
    spread = np.percentile(ratio, 75, axis=0) - np.percentile(ratio, 25, axis=0)
    if np.any(np.abs(gain - 1.0) > 0.08) or np.any(spread > 0.04):
        return None                                        # not the same picture (film moved or changed)
    return tuple(float(g) for g in gain)


def adopt_scan(rgb, ir, dpi, gain=None):
    """Make sensor data the scan in memory (cut to the film crop). Caller holds S.lock."""
    frame = film.Settings.from_dict(S.settings.to_dict())
    S.full_rgb, S.full_ir = bake(rgb, ir, frame)
    S.level_match = gain
    if gain is not None and any(abs(g - 1.0) > 0.001 for g in gain):
        for c in range(3):
            S.full_rgb[..., c] = np.clip(S.full_rgb[..., c] * np.float32(gain[c]) + 0.5, 0, 65535).astype(np.uint16)
    S.full_dpi = dpi
    S.art_crop = (0.0, 0.0, 1.0, 1.0)
    S.preview_rgb, S.preview_ir = small_copy(S.full_rgb, dpi), small_copy(S.full_ir, dpi)
    S.version += 1


def turn_scan(turn, flip_h, flip_v):
    """Rotate / flip the scan in memory. Caller holds S.lock."""
    def t(a):
        if a is None:
            return None
        if flip_h:
            a = a[:, ::-1]
        if flip_v:
            a = a[::-1]
        if turn:
            a = np.rot90(a, -turn)
        return np.ascontiguousarray(a)
    S.full_rgb, S.full_ir, S.preview_rgb, S.preview_ir = t(S.full_rgb), t(S.full_ir), t(S.preview_rgb), t(S.preview_ir)
    S.art_crop = (0.0, 0.0, 1.0, 1.0)


def after_new_frame():
    """New film in the gate: measure what is allowed to be measured. Caller holds S.lock."""
    if not S.base_locked:
        S.settings.base = film.measure_base(S.preview_rgb)
    if S.auto_exposure:
        S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)


def next_filename(ext):
    os.makedirs(S.out_dir, exist_ok=True)
    day = datetime.date.today().isoformat()
    n = 1
    existing = [f for f in os.listdir(S.out_dir) if f.startswith(day + "-")]
    for f in existing:
        try:
            n = max(n, int(f[len(day) + 1:len(day) + 5]) + 1)
        except ValueError:
            pass
    return os.path.join(S.out_dir, f"{day}-{n:04d}.{ext}")


_ICC = None


def srgb_icc():
    global _ICC
    if _ICC is None:
        from PIL import ImageCms
        _ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _ICC


def write_raw(rgb, ir, dpi, path):
    """Untouched 16-bit linear sensor data: RGB, plus infrared as a fourth channel when present."""
    data = rgb if ir is None else np.dstack([rgb, ir])
    tifffile.imwrite(path, data, photometric="rgb", extrasamples=() if ir is None else ("unspecified",),
                     resolution=(dpi, dpi), resolutionunit="INCH", metadata=None, software="filmscan raw")


def write_output(rgb, ir, settings, fmt, dpi, path=None):
    space = "srgb" if settings.mono else S.colorspace
    y = film.develop(rgb, ir, settings, out_bits=0, ref_width=int(1.44 * dpi))
    ext = "jpg" if fmt == "jpeg" else "tif"
    path = path or next_filename(ext)
    entry = colorspace.find_profile(space)
    if entry is not None:
        # An installed print profile (CMYK or RGB). Converted by the colour engine at 8 bits per channel,
        # whatever bit depth is selected, with the profile embedded.
        data, mode, icc_bytes = colorspace.convert_with_profile(y, entry)
        del y
        img = Image.fromarray(data, mode)
        if fmt == "jpeg":
            img.save(path, quality=int(S.jpeg_quality), subsampling=0, dpi=(dpi, dpi), icc_profile=icc_bytes)
        else:
            img.save(path, compression="tiff_adobe_deflate" if fmt == "tiff8" else None, dpi=(dpi, dpi), icc_profile=icc_bytes)
        return path
    if space.startswith("icc:"):                           # the chosen profile is no longer installed
        space = "srgb"
    out = film.quantise(colorspace.convert(y, space), 16 if fmt == "tiff16" else 8)
    del y
    if settings.mono:
        out = np.ascontiguousarray(out[..., 0])           # black-and-white saves as a single grey channel
    if fmt == "jpeg":
        Image.fromarray(out).save(path, quality=int(S.jpeg_quality), subsampling=0, dpi=(dpi, dpi),
                                  **({} if settings.mono else {"icc_profile": colorspace.icc(space)}))
    else:
        tifffile.imwrite(path, out, photometric="minisblack" if settings.mono else "rgb",
                         compression="zlib" if fmt == "tiff8" else None,
                         resolution=(dpi, dpi), resolutionunit="INCH",
                         metadata=None, software="filmscan",
                         extratags=[] if settings.mono else [(34675, 7, None, colorspace.icc(space), True)])
    return path


def log_line(text):
    try:
        with open(os.path.join(HERE, "scan.log"), "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def job_scan():
    with S.lock:
        dpi, infrared, fmt = int(S.scan_dpi), bool(S.infrared) and not S.settings.mono, S.fmt
        samples = max(1, min(16, int(S.samples)))
        long_level = int(S.multi_exposure) if int(S.multi_exposure) in (2, 3, 4) and dpi >= 900 else 0
    sc = get_scanner()
    set_job(0.01, "calibrating")
    labels = [f"scanning, pass {i + 1} of {samples}" if samples > 1 else "scanning" for i in range(samples)]
    if long_level:
        labels.append(f"long exposure pass ({long_level}x)")
    if infrared:
        labels.append("infrared pass")

    def prog(f):
        set_job(0.05 + 0.80 * f, labels[min(len(labels) - 1, int(f * len(labels)))])

    r = sc.scan(dpi=dpi, infrared=infrared, samples=samples, multi_exposure=long_level, progress=prog, cancel=cancelled)
    if S.cancel:
        raise Cancelled()
    if r["rgb"] is None or r["rgb"].shape[0] < r["info"]["lines"]:
        raise RuntimeError("scan ended early")
    set_job(0.87, "developing")
    with S.lock:
        sensor_preview = S.preview_rgb if S.full_rgb is None else None      # only a preview of the whole sensor frame counts
    gain = None
    if sensor_preview is not None:
        try:
            gain = level_gain(r["rgb"], sensor_preview)
        except Exception:
            gain = None
        log_line("level match scan->preview: " + ("none" if gain is None else " ".join(f"{g:.4f}" for g in gain)))
    with S.lock:
        keep_raw = S.save_raw
        adopt_scan(r["rgb"], r["ir"], dpi, gain)                 # the scan is the film crop of what the sensor saw
        rgb, ir, settings = S.full_rgb, S.full_ir, whole(active_settings())
    path = write_output(rgb, ir, settings, fmt, dpi)
    if keep_raw:
        set_job(0.95, "saving raw")
        write_raw(r["rgb"], r["ir"], dpi, os.path.splitext(path)[0] + "_raw.tif")     # raw keeps the whole sensor frame
    with S.lock:
        S.last_file, S.last_crop, S.last_version = path, None, None
    set_job(0.97, "preparing view")
    render_fit()                                           # build the full-resolution view cache now, not on first zoom
    set_job(1.0, f"saved {os.path.basename(path)}")


def scan_in_memory():
    with S.lock:
        if S.full_rgb is None:
            raise RuntimeError("No scan in memory. Scan the frame (or open a raw scan) first.")
        return (S.full_rgb, S.full_ir, S.full_dpi, S.fmt, active_settings(), S.last_file)


def job_reprocess():
    """Save over the scan file with the current colour settings. The artistic crop does not apply here."""
    rgb, ir, dpi, fmt, settings, last = scan_in_memory()
    ext = ".jpg" if fmt == "jpeg" else ".tif"
    path = last if last and last.lower().endswith(ext) else None
    set_job(0.2, "developing")
    path = write_output(rgb, ir, whole(settings), fmt, dpi, path)
    with S.lock:
        S.last_file = path
    set_job(1.0, f"saved {os.path.basename(path)}")


def job_save_version():
    """Save the whole scan with the current settings as a new file next to it: <name>_v2.<ext>, _v3 ...
    The scan file itself is left as it is."""
    rgb, ir, dpi, fmt, settings, last = scan_in_memory()
    ext = "jpg" if fmt == "jpeg" else "tif"
    stem = os.path.splitext(last)[0] if last else os.path.splitext(next_filename(ext))[0]
    n = 2
    while any(os.path.exists(f"{stem}_v{n}.{e}") for e in ("tif", "jpg")):
        n += 1
    path = f"{stem}_v{n}.{ext}"
    set_job(0.2, "developing version")
    write_output(rgb, ir, whole(settings), fmt, dpi, path)
    with S.lock:
        S.last_version = path
    set_job(1.0, f"saved {os.path.basename(path)}")


def job_save_crop():
    """Save the area inside the crop box (the artistic crop) as its own file next to the scan: <name>_crop.<ext>."""
    rgb, ir, dpi, fmt, settings, last = scan_in_memory()
    ext = "jpg" if fmt == "jpeg" else "tif"
    stem = os.path.splitext(last)[0] if last else os.path.splitext(next_filename(ext))[0]
    path = stem + "_crop." + ext
    set_job(0.2, "developing crop")
    write_output(rgb, ir, settings, fmt, dpi, path)
    with S.lock:
        S.last_crop = path
    set_job(1.0, f"saved {os.path.basename(path)}")


# -- preview rendering ----------------------------------------------------------

# Cache, three levels, each rebuilt only when what it depends on changes:
#   heavy    healed sensor data at full resolution (film base, film type, dust settings)     seconds for a 3600 dpi scan
#   geometry the same, flipped / rotated / straightened                                      fast
#   fit      shrunk to screen size                                                           fast
_disp = dict(heavy_key=None, healed=None, holder0=None, scale=1.0,
             geo_key=None, full=None, holder_full=None, fit=None, holder_fit=None, fit_f=1.0)
_disp_lock = threading.Lock()
_last = dict(version=-1, img=None)


def _sources():
    """Settings snapshot plus the best raw data in memory. Returns None when there is nothing to show."""
    with S.lock:
        if S.preview_rgb is None:
            return None
        if S.full_rgb is not None:
            rgb, ir = S.full_rgb, S.full_ir
            ref_w = int(1.44 * S.full_dpi)                 # sizes (sharpen, dust) follow the scan resolution, not the crop
        else:
            rgb, ir = S.preview_rgb, S.preview_ir
            ref_w = rgb.shape[1]
        s = active_settings()
        s._ref_w = ref_w
        return rgb, ir, s, S.version


def _ensure(rgb, ir, s):
    """Bring the cache up to date. Caller holds _disp_lock."""
    heavy_key = (id(rgb), id(ir), s.negative, s.mono, s.holder, tuple(s.base), s.dust, s.dust_threshold, s.dust_grow)
    if _disp["heavy_key"] != heavy_key:
        raw, holder, scale = film.prepare(rgb, ir, s, ref_width=getattr(s, "_ref_w", rgb.shape[1]), apply_geometry=False)
        healed, _, _ = film.display_source(raw, None, s, max_side=10 ** 9)       # uint16, full size, mono folded in
        _disp.update(heavy_key=heavy_key, healed=healed, holder0=holder, scale=scale, geo_key=None)
    geo_key = (s.flip_h, s.flip_v, s.rotate, s.angle)
    if _disp["geo_key"] != geo_key:
        full = np.ascontiguousarray(film.orient(_disp["healed"], s))
        hf = None
        if _disp["holder0"] is not None:
            hf = np.ascontiguousarray(film.orient(_disp["holder0"].astype(np.uint8), s)).astype(bool)
        h, w = full.shape[:2]
        f = min(1.0, 1100 / max(h, w))
        if f < 1.0:
            size = (max(1, int(w * f)), max(1, int(h * f)))
            fit = cv2.resize(full, size, interpolation=cv2.INTER_AREA)
            hfit = None if hf is None else cv2.resize(hf.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)
        else:
            fit, hfit = full, hf
        _disp.update(geo_key=geo_key, full=full, holder_full=hf, fit=fit, holder_fit=hfit, fit_f=f)


HOLDER_GREY = (44, 44, 50)        # on screen the holder is grey so it cannot be mistaken for a black picture edge


def _for_screen(s, boost):
    """Display-only changes. boost: brighten hard so a dark frame's boundary can be found."""
    if boost:
        s.exposure += 3.5
        s.contrast = 0.0
        s.shadows = 1.0
        s.black = 0.0
    return s


def _paint_holder(img, holder):
    if holder is not None:
        img[holder] = HOLDER_GREY
    return img


def render_fit(boost=False, plain=False):
    """Whole frame at screen size (uint8). Slider moves only redo the cheap tone step."""
    got = _sources()
    if got is None:
        return None
    rgb, ir, s, version = got
    with _disp_lock:
        _ensure(rgb, ir, s)
        # plain: nothing is masked, so clear film next to the holder can be seen and clicked (picking the film base)
        holder = None if plain else _disp["holder_fit"]
        img = film.finish_display(_disp["fit"], holder, _for_screen(s, boost), _disp["scale"] * _disp["fit_f"])
        if not boost and not plain:
            _last.update(version=version, img=img.copy())          # histogram reads the true picture
        img = _paint_holder(img, holder)
    return img


render_preview = render_fit


def render_view(x0, y0, x1, y1, out_w, out_h, boost=False):
    """Part of the frame (fractions of the displayed frame), at most out_w x out_h pixels, never above 1:1."""
    got = _sources()
    if got is None:
        return None
    rgb, ir, s, _ = got
    with _disp_lock:
        _ensure(rgb, ir, s)
        full, hf = _disp["full"], _disp["holder_full"]
        H, W = full.shape[:2]
        xa, xb = int(np.clip(round(x0 * W), 0, W - 1)), int(np.clip(round(x1 * W), 1, W))
        ya, yb = int(np.clip(round(y0 * H), 0, H - 1)), int(np.clip(round(y1 * H), 1, H))
        xb, yb = max(xb, xa + 1), max(yb, ya + 1)
        src = full[ya:yb, xa:xb]
        hm = None if hf is None else hf[ya:yb, xa:xb]
        f = min(1.0, out_w / src.shape[1], out_h / src.shape[0])
        if f < 1.0:
            size = (max(1, int(src.shape[1] * f)), max(1, int(src.shape[0] * f)))
            src = cv2.resize(src, size, interpolation=cv2.INTER_AREA)
            hm = None if hm is None else cv2.resize(hm.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)
        img = film.finish_display(np.ascontiguousarray(src), hm, _for_screen(s, boost), _disp["scale"] * f)
        return _paint_holder(img, hm)


def oriented_preview():
    with S.lock:
        s = whole(active_settings())
        return film.orient(S.preview_rgb, s), s


# -- routes ---------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    return jsonify(S.public())


def apply_settings(d):
    """Merge a settings patch from the UI. Caller holds S.lock."""
    cur = S.settings.to_dict()
    patch = dict(d.get("settings") or {})
    if S.full_rgb is not None:
        # A scan is on screen. The crop box is the artistic crop; rotate/flip act on the scan itself
        # (and are remembered for the next preview); straightening belongs to the preview.
        if "crop" in patch:
            S.art_crop = tuple(float(v) for v in patch.pop("crop"))
        patch.pop("angle", None)
        turn = (int(patch["rotate"]) - int(cur["rotate"])) % 4 if "rotate" in patch else 0
        fh = "flip_h" in patch and bool(patch["flip_h"]) != bool(cur["flip_h"])
        fv = "flip_v" in patch and bool(patch["flip_v"]) != bool(cur["flip_v"])
        if turn or fh or fv:
            turn_scan(turn, fh, fv)
    for k, v in patch.items():
        if k in cur:
            cur[k] = v
    S.settings = film.Settings.from_dict(cur)
    for k in ("base_locked", "auto_exposure", "out_dir", "scan_dpi", "preview_dpi", "infrared", "fmt", "save_raw", "jpeg_quality", "samples", "colorspace", "multi_exposure"):
        if k in d:
            setattr(S, k, d[k])
    S.version += 1


_save_timer = None


def save_soon():
    """Write settings.json shortly after the last change instead of on every slider tick."""
    global _save_timer
    if _save_timer is not None:
        _save_timer.cancel()

    def go():
        with S.lock:
            S.save()
    _save_timer = threading.Timer(0.8, go)
    _save_timer.daemon = True
    _save_timer.start()


@app.route("/api/image", methods=["GET", "POST"])
def api_image():
    """GET: current picture. POST: apply a settings patch and return the new picture in one round trip."""
    if request.method == "POST":
        with S.lock:
            apply_settings(request.get_json(force=True))
        save_soon()
    with S.lock:
        version = S.version
    img = render_fit(boost=request.args.get("boost") == "1", plain=request.args.get("plain") == "1")
    if img is None:
        return ("", 204)
    return _jpeg(img, version)


@app.get("/api/histogram")
def api_histogram():
    with S.lock:
        version = S.version
    if _last["img"] is None or _last["version"] != version:
        # The zoomed view, "Find edges" and base picking draw the picture without going through the whole-frame
        # render the histogram reads from, so bring that up to date first.
        render_fit()
    img = _last["img"]
    if img is None:
        return jsonify(None)
    with S.lock:
        crop = active_settings().crop
    img = film.crop_frac(img, crop)
    return jsonify({c: np.bincount(img[..., i].ravel() >> 2, minlength=64).tolist() for i, c in enumerate("rgb")})


@app.post("/api/settings")
def api_settings():
    with S.lock:
        apply_settings(request.get_json(force=True))
    save_soon()
    return jsonify(S.public())


@app.post("/api/pick")
def api_pick():
    """Click on the displayed (full, oriented) frame. mode: 'base' or 'neutral'."""
    d = request.get_json(force=True)
    with S.lock:
        if S.preview_rgb is None:
            return jsonify(error="no preview yet"), 400
        view, s = oriented_preview()
        if d["mode"] == "base":
            S.settings.base = film.measure_base_at(view, float(d["x"]), float(d["y"]))
            S.base_locked = True
        else:
            wb = film.neutral_wb_at(S.preview_rgb, s, float(d["x"]), float(d["y"]))
            if tuple(wb) == tuple(s.wb):
                return jsonify(error="That spot has no image in at least one colour layer, so it cannot be used as "
                                     "a neutral. Pick something grey or white that is clearly lit."), 400
            S.settings.wb = wb
        if S.auto_exposure:
            S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)
        S.version += 1
        S.save()
    return jsonify(S.public())


@app.post("/api/auto")
def api_auto():
    d = request.get_json(force=True)
    with S.lock:
        if S.preview_rgb is None:
            return jsonify(error="no preview yet"), 400
        what = d.get("what")
        if what == "base":
            S.settings.base = film.measure_base(S.preview_rgb)
            S.base_locked = True
        elif what == "exposure":
            S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)
        elif what in ("crop", "angle"):
            if S.full_rgb is not None:
                return jsonify(error="Fitting to the film holder works on a preview. This is a finished scan: "
                                     "drag the box for an artistic crop, or run a new preview."), 400
            if what == "angle":
                S.settings.angle = film.auto_angle(S.preview_rgb, S.settings)
            S.settings.crop = film.auto_crop(S.preview_rgb, S.settings)
        elif what == "restore":
            wb = film.restore_wb(S.preview_rgb, active_settings())
            if wb is None:
                return jsonify(error="Not enough image in every colour layer to rebalance this frame."), 400
            S.settings.wb = wb
            if S.auto_exposure:
                S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)
        elif what == "reset_color":
            keep = S.settings
            fresh = film.Settings()
            for k in ("base", "negative", "mono", "flip_h", "flip_v", "rotate", "angle", "crop", "dust", "dust_threshold", "dust_grow"):
                setattr(fresh, k, getattr(keep, k))
            S.settings = fresh
            S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)
        S.version += 1
        S.save()
    return jsonify(S.public())


def _jpeg(img, version, quality=95):
    # Full-resolution colour (4:4:4). The default halves colour resolution, which smears saturated
    # reds against black into blocky artefacts.
    ok, enc = cv2.imencode(".jpg", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, quality,
                            cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444])
    resp = app.response_class(enc.tobytes(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Version"] = str(version)
    return resp


@app.route("/api/view", methods=["GET", "POST"])
def api_view():
    """Zoomed view. Query: x0,y0,x1,y1 (fractions of the displayed frame), w,h (screen pixels available).
    POST may carry a settings patch, like /api/image."""
    if request.method == "POST":
        with S.lock:
            apply_settings(request.get_json(force=True))
        save_soon()
    a = request.args
    with S.lock:
        version = S.version
    img = render_view(float(a.get("x0", 0)), float(a.get("y0", 0)), float(a.get("x1", 1)), float(a.get("y1", 1)),
                      max(16, int(float(a.get("w", 1200)))), max(16, int(float(a.get("h", 800)))),
                      boost=a.get("boost") == "1")
    if img is None:
        return ("", 204)
    return _jpeg(img, version)


# Looks and per-film settings. Built-ins change tone only; saved presets also carry film base and balance.
BUILTIN_PRESETS = {
    "Look: neutral": dict(contrast=0.25, gamma=1.6, saturation=1.0, shadows=0.0, highlights=0.0, curve={}),
    "Look: punchy": dict(contrast=0.45, gamma=1.75, saturation=1.15, shadows=-0.1, highlights=0.1),
    "Look: soft": dict(contrast=0.10, gamma=1.45, saturation=0.95, shadows=0.2, highlights=-0.15),
}
PRESET_KEYS = ("negative", "mono", "base", "wb", "gamma", "contrast", "black", "white", "shadows", "highlights", "curve",
               "saturation", "temp", "tint", "shadow_temp", "shadow_tint", "sharpen", "grain", "dust_threshold", "dust_grow")


def load_presets():
    if not os.path.exists(PRESET_FILE):                   # first run: start from the presets that ship with the app
        try:
            import shutil
            shutil.copyfile(os.path.join(HERE, "presets.default.json"), PRESET_FILE)
        except OSError:
            pass
    try:
        return json.load(open(PRESET_FILE, encoding="utf-8"))
    except Exception:
        return {}


@app.route("/api/presets", methods=["GET", "POST"])
def api_presets():
    """GET: names. POST {action: save|apply|delete, name}."""
    user = load_presets()
    if request.method == "POST":
        d = request.get_json(force=True)
        name, action = (d.get("name") or "").strip(), d.get("action")
        if not name:
            return jsonify(error="give the preset a name"), 400
        with S.lock:
            if action == "save":
                if name in BUILTIN_PRESETS:
                    return jsonify(error="that name belongs to a built-in look"), 400
                cur = S.settings.to_dict()
                user[name] = {k: cur[k] for k in PRESET_KEYS}
            elif action == "delete":
                if name not in user:
                    return jsonify(error="only your own presets can be deleted"), 400
                del user[name]
            elif action == "apply":
                vals = BUILTIN_PRESETS.get(name) or user.get(name)
                if vals is None:
                    return jsonify(error="no such preset"), 404
                cur = S.settings.to_dict()
                cur.update({k: v for k, v in vals.items() if k in cur})
                S.settings = film.Settings.from_dict(cur)
                if name in user:
                    S.base_locked = True                 # a film preset brings its own base: hold it
                S.version += 1
                S.save()
            if action in ("save", "delete"):
                tmp = PRESET_FILE + ".tmp"
                json.dump(user, open(tmp, "w"), indent=1)
                os.replace(tmp, PRESET_FILE)
    return jsonify(builtin=list(BUILTIN_PRESETS), user=sorted(user), state=S.public(),
                   bases={k: v["base"] for k, v in user.items() if isinstance(v, dict) and v.get("base")})


def load_raw_file(path):
    """Open a raw file written by this app (16-bit RGB or RGB+infrared TIFF) for developing again."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        data = page.asarray()
        dpi = 3600
        try:
            num, den = page.tags["XResolution"].value
            dpi = int(round(num / den)) or 3600
        except Exception:
            pass
    if data.dtype != np.uint16 or data.ndim != 3 or data.shape[2] not in (3, 4):
        raise RuntimeError("not a raw scan from this app (needs 16-bit RGB or RGB + infrared)")
    rgb = np.ascontiguousarray(data[..., :3])
    ir = np.ascontiguousarray(data[..., 3]) if data.shape[2] == 4 else None
    with S.lock:
        if not S.base_locked:
            S.settings.base = film.measure_base(small_copy(rgb, dpi))
        adopt_scan(rgb, ir, dpi)
        S.last_file = S.last_crop = S.last_version = None
        if S.auto_exposure:
            S.settings.exposure = round(film.auto_exposure(S.preview_rgb, active_settings()), 2)
        S.save()


@app.post("/api/open_raw")
def api_open_raw():
    """Load a saved raw scan. Body {path}; without a path a file dialog is shown (native window only)."""
    d = request.get_json(force=True) or {}
    path = (d.get("path") or "").strip().strip('"')
    if not path:
        if S.window is None:
            return jsonify(error="type the full path of the raw file"), 400
        import webview
        picked = S.window.create_file_dialog(webview.OPEN_DIALOG, directory=S.out_dir,
                                             file_types=("Raw scans (*_raw.tif;*.tif;*.tiff)", "All files (*.*)"))
        if not picked:
            return jsonify(S.public())
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
    with S.lock:
        if S.job["state"] == "running":
            return jsonify(error="wait for the running job to finish"), 409
    try:
        load_raw_file(path)
    except Exception as e:
        return jsonify(error=f"could not open {os.path.basename(path)}: {e}"), 400
    return jsonify(S.public())


@app.post("/api/job")
def api_job():
    kind = request.get_json(force=True).get("kind")
    fn = dict(preview=job_preview, scan=job_scan, reprocess=job_reprocess, save_crop=job_save_crop, save_version=job_save_version).get(kind)
    if fn is None:
        return jsonify(error="unknown job"), 400
    if not start_job(kind, fn):
        return jsonify(error="scanner is busy"), 409
    return jsonify(S.public())


@app.post("/api/cancel")
def api_cancel():
    """Stop the running scan. The scanner finishes its current step (calibration cannot be interrupted)."""
    with S.lock:
        if S.job["state"] == "running" and S.job["kind"] in ("scan", "preview"):
            S.cancel = True
            S.job["message"] = "cancelling..."
    return jsonify(S.public())


@app.post("/api/connect")
def api_connect():
    try:
        get_scanner()
    except Exception:
        pass
    return jsonify(S.public())


@app.post("/api/open_folder")
def api_open_folder():
    with S.lock:
        target = S.last_file if S.last_file and os.path.exists(S.last_file) else S.out_dir
    try:
        if os.path.isfile(target):
            os.startfile(os.path.dirname(target))
        else:
            os.makedirs(target, exist_ok=True)
            os.startfile(target)
    except OSError as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


def already_running():
    import socket
    with socket.socket() as k:
        k.settimeout(0.5)
        return k.connect_ex(("127.0.0.1", PORT)) == 0


def shutdown():
    """Release the scanner and end the process."""
    try:
        with S.lock:
            S.save()
            if S.scanner is not None:
                S.scanner.close()
    finally:
        os._exit(0)


@app.post("/api/quit")
def api_quit():
    with S.lock:
        if S.job["state"] == "running":
            return jsonify(error="a scan is running; wait for it to finish"), 409
    threading.Timer(0.3, shutdown).start()
    return jsonify(ok=True)


def serve():
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


def main():
    url = f"http://127.0.0.1:{PORT}"
    owner = not already_running()              # a second launch only opens another window
    if owner:
        threading.Thread(target=keepalive, daemon=True).start()
        try:
            get_scanner()
        except Exception as e:
            print("scanner:", e)
    if os.environ.get("FILMSCAN_NO_BROWSER") == "1":
        if owner:
            serve()
        return
    if owner:
        threading.Thread(target=serve, daemon=True).start()
        for _ in range(50):
            if already_running():
                break
            time.sleep(0.1)
    try:
        import webview
    except ImportError:
        webview = None
    if webview is None:                        # no native window available: fall back to the browser
        webbrowser.open(url)
        if owner:
            while True:
                time.sleep(3600)
        return
    S.window = webview.create_window("Film Scan", url, width=1500, height=980, min_size=(1000, 700), background_color="#121212")
    webview.start()                            # returns when the window is closed
    if owner:
        shutdown()


if __name__ == "__main__":
    main()
