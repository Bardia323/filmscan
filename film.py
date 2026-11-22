"""Colour pipeline for scanned colour negatives (and slides).

One function, `develop`, turns linear sensor data into a display image. The preview and the
final scan both go through it with the same settings, so they always match.

Model for negatives
    t      = raw / film_base            transmission relative to clear film (orange mask removed)
    d      = -log10(t)                  density above base, per channel
    d     *= wb                         per-channel balance (default 1,1,1: nothing is neutralised)
    scene  = 10 ** (d * gamma) * gain   back to scene-linear light; gamma ~ 1 / film contrast
    out    = tone curve + sRGB encoding

Nothing here is estimated per frame unless asked for: film base is measured once per roll and held.
"""
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np


@dataclass
class Settings:
    negative: bool = True
    mono: bool = False                         # black-and-white film: one neutral channel, no infrared cleaning
    base: tuple = (32000.0, 14000.0, 9000.0)   # raw value of clear film, per channel
    wb: tuple = (1.0, 1.0, 1.0)                # density multipliers (R, G, B)
    exposure: float = 0.0                      # stops
    gamma: float = 1.6                         # density -> scene exponent (film contrast inverse)
    black: float = 0.0                         # black point, fraction of scene range
    shadows: float = 0.0                       # curve: lift (+) or deepen (-) the dark tones
    highlights: float = 0.0                    # curve: brighten (+) or hold back (-) the light tones
    curve: dict = field(default_factory=dict)  # tone curves: {'rgb'|'r'|'g'|'b': [[x, y], ...]}, values 0..1
    angle: float = 0.0                         # straighten, degrees clockwise
    white: float = 1.0                         # white point: scene level that becomes full white
    contrast: float = 0.25                     # 0 = flat, 1 = strong S-curve
    saturation: float = 1.0
    grain: float = 0.0                         # grain reduction strength, 0 = off, 1 = strong
    sharpen: float = 0.4                       # unsharp mask amount, 0 = off
    temp: float = 0.0                          # warm(+)/cool(-), applied in scene-linear
    tint: float = 0.0                          # magenta(+)/green(-)
    flip_h: bool = False
    flip_v: bool = False
    rotate: int = 0                            # quarter turns clockwise
    crop: tuple = (0.0, 0.0, 1.0, 1.0)         # x0, y0, x1, y1 as fractions of the scan area
    dust: bool = True
    holder: bool = True                        # look for the film holder at the edges and render it black
    dust_threshold: float = 0.07               # IR drop (fraction) that counts as a defect
    dust_grow: float = 2.5                     # mask growth in pixels at 3600 dpi
    shadow_temp: float = 0.0                   # colour of the dark tones: warm(+)/cool(-), an offset per channel
    shadow_tint: float = 0.0                   # colour of the dark tones: magenta(+)/green(-)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, tuple(v) if isinstance(v, list) else v)
        return s


# -- measurements ---------------------------------------------------------------

def measure_base(rgb, top=0.004):
    """Clear-film value per channel: the clearest patch of film in the raw scan.

    Works on a smoothed copy. A plain high percentile of single pixels lands about three noise
    sigmas above the true level, which leaves a coloured haze in the shadows."""
    step = max(1, rgb.shape[1] // 1300)
    small = rgb[::step, ::step].astype(np.float32)
    k = max(5, int(small.shape[1] * 0.012)) | 1
    sm = cv2.blur(small, (k, k))
    lum = sm.sum(axis=2)
    sel = lum >= np.quantile(lum, 1.0 - top)
    return tuple(float(v) for v in np.median(sm[sel], axis=0))


def measure_base_at(rgb, fx, fy, radius=0.01):
    """Clear-film value from a patch the user clicked (fractions of the raw frame)."""
    h, w = rgb.shape[:2]
    r = max(2, int(radius * w))
    x, y = int(fx * w), int(fy * h)
    patch = rgb[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1].reshape(-1, 3).astype(np.float32)
    return tuple(float(v) for v in np.median(patch, axis=0))


# -- geometry -------------------------------------------------------------------

def orient(a, s):
    if s.flip_h:
        a = a[:, ::-1]
    if s.flip_v:
        a = a[::-1]
    if s.rotate % 4:
        a = np.rot90(a, -(s.rotate % 4))
    if abs(s.angle) > 0.01:
        a = np.ascontiguousarray(a)
        h, w = a.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -s.angle, 1.0)
        a = cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return a


def crop_frac(a, crop):
    h, w = a.shape[:2]
    x0, y0, x1, y1 = crop
    xa, xb = int(round(min(x0, x1) * w)), int(round(max(x0, x1) * w))
    ya, yb = int(round(min(y0, y1) * h)), int(round(max(y0, y1) * h))
    xb, yb = max(xb, xa + 8), max(yb, ya + 8)
    return a[max(0, ya):min(h, yb), max(0, xa):min(w, xb)]


# -- infrared dust removal ------------------------------------------------------

def dust_mask(ir, threshold=0.12, grow=1.5, scale=1.0):
    """Defects block infrared; dyes do not. Mask = IR clearly below its local background."""
    irf = ir.astype(np.float32)
    k = int(max(15, 61 * scale)) | 1
    bg = cv2.dilate(cv2.resize(irf, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA),
                    np.ones((max(3, k // 4) | 1,) * 2, np.uint8))
    bg = cv2.GaussianBlur(bg, (0, 0), max(2.0, k / 8))
    bg = cv2.resize(bg, (ir.shape[1], ir.shape[0]), interpolation=cv2.INTER_LINEAR)
    m = (irf < bg * (1.0 - threshold)).astype(np.uint8)
    g = max(1, int(round(grow * scale)))
    m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
    return m


def heal(img, mask, scale=1.0):
    """Fill masked pixels from their surroundings (works on float32, any channel count)."""
    if not mask.any():
        return img
    out = cv2.inpaint(img, mask, max(2.0, 3.0 * scale), cv2.INPAINT_TELEA) if img.dtype == np.uint8 else None
    if out is not None:
        return out
    valid = (mask == 0).astype(np.float32)
    res = img.copy()
    todo = mask.astype(bool)
    for sigma in (1.5 * scale, 3 * scale, 6 * scale, 14 * scale, 40 * scale):
        sigma = max(1.0, sigma)
        wsum = cv2.GaussianBlur(valid, (0, 0), sigma)
        num = cv2.GaussianBlur(img * valid[..., None], (0, 0), sigma)
        ok = todo & (wsum > 0.2)
        if ok.any():
            res[ok] = num[ok] / wsum[ok][:, None]
            todo &= ~ok
        if not todo.any():
            break
    return res


# -- colour ---------------------------------------------------------------------

_SRGB_LUT = None


def _srgb_encode(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _tone(x, contrast):
    """Soft shoulder for highlights, then an S-curve around mid grey. x is scene-linear, 1.0 = white."""
    x = np.maximum(x, 0.0)
    x = x / (1.0 + x * 0.25) * 1.25            # highlights roll off instead of clipping hard
    y = _srgb_encode(x)
    if contrast > 0:
        k = 1.0 + 3.0 * contrast
        y = np.clip(y, 0.0, 1.0)
        s = np.where(y < 0.5, 0.5 * np.power(2 * y, k), 1 - 0.5 * np.power(2 * (1 - y), k))
        y = y * (1 - contrast) + s * contrast
    return y


def _scene_from_t(t, s):
    """t: transmission relative to clear film, float32 [..., 3] -> scene-linear. Per-channel maths only."""
    t = np.clip(t, 1e-5, None)
    if s.negative:
        d = -np.log10(t) * (1.0 if s.mono else np.asarray(s.wb, np.float32))
        scene = np.power(10.0, d * s.gamma) - 1.0          # clear film -> black
        scene *= 0.18 / 4.0                                # density 0.42 (mid tone) -> ~mid grey
    else:
        scene = t
    gain = 2.0 ** s.exposure
    if s.mono:
        scene = scene * gain
    else:
        wbt = np.array([2.0 ** (s.temp * 0.5 + s.tint * 0.15), 2.0 ** (-s.tint * 0.3),
                        2.0 ** (-s.temp * 0.5 + s.tint * 0.15)], np.float32)
        scene = scene * (gain * wbt)
        if s.shadow_temp or s.shadow_tint:
            # A per-channel offset: colours the dark tones and leaves the light ones almost alone. This is what a
            # shifted film base does to the picture, offered as a control so the base can stay a measurement.
            a, b = SHADOW_RANGE * s.shadow_temp, SHADOW_RANGE * s.shadow_tint
            scene = scene + np.array([a + 0.5 * b, -b, -a + 0.5 * b], np.float32)
    if s.black or s.white != 1.0:
        scene = (scene - s.black) / max(1e-3, s.white - s.black)
    return scene.astype(np.float32)


_CURVE_N = 4096
SHADOW_RANGE = 0.05                # scene-linear offset at a shadow colour slider's end (mid grey is 0.18)


def curve_points(pts):
    """Clean a list of [x, y] curve points: sorted, inside 0..1, strictly increasing in x. None if unusable."""
    try:
        p = sorted((min(1.0, max(0.0, float(a))), min(1.0, max(0.0, float(b)))) for a, b in pts)
    except (TypeError, ValueError):
        return None
    out = []
    for x, y in p:
        if out and x - out[-1][0] < 1e-4:
            continue
        out.append((x, y))
    return out if len(out) >= 2 else None


def curve_table(pts, n=_CURVE_N):
    """Smooth curve through the points, sampled at n inputs in 0..1 (monotone cubic: it never overshoots
    between points, so a rising set of points gives a rising curve). None for a straight identity line."""
    p = curve_points(pts or [])
    if p is None or all(abs(x - y) < 1e-6 for x, y in p):
        return None
    xs = np.array([a for a, _ in p], np.float64)
    ys = np.array([b for _, b in p], np.float64)
    h = np.diff(xs)
    d = np.diff(ys) / h
    m = np.empty_like(xs)
    m[0], m[-1] = d[0], d[-1]
    if len(xs) > 2:
        same = d[:-1] * d[1:] > 0
        m[1:-1] = np.where(same, 2 * d[:-1] * d[1:] / np.where(same, d[:-1] + d[1:], 1.0), 0.0)
    x = np.linspace(0.0, 1.0, n)
    i = np.clip(np.searchsorted(xs, x, side="right") - 1, 0, len(xs) - 2)
    t = np.clip((x - xs[i]) / h[i], 0.0, 1.0)
    y = ((2 * t ** 3 - 3 * t ** 2 + 1) * ys[i] + (t ** 3 - 2 * t ** 2 + t) * h[i] * m[i]
         + (-2 * t ** 3 + 3 * t ** 2) * ys[i + 1] + (t ** 3 - t ** 2) * h[i] * m[i + 1])
    y = np.where(x < xs[0], ys[0], np.where(x > xs[-1], ys[-1], y))       # flat outside the first / last point
    return np.clip(y, 0.0, 1.0)


def _apply_curves(y, s):
    """Apply the master curve, then the per-channel ones. y: encoded 0..1, last axis = RGB (or any shape for mono)."""
    grid = np.linspace(0.0, 1.0, _CURVE_N)
    master = curve_table(s.curve.get("rgb"))
    per = None if s.mono else [curve_table(s.curve.get(k)) for k in "rgb"]
    if master is None and (per is None or all(t is None for t in per)):
        return y
    y = np.clip(y, 0.0, 1.0)
    if per is None or y.ndim < 2 or y.shape[-1] != 3:
        return np.interp(y, grid, master).astype(np.float32) if master is not None else y
    out = np.empty(y.shape, np.float32)
    for c in range(3):
        t = master if master is not None else grid
        if per[c] is not None:
            t = np.interp(t, grid, per[c])                               # channel curve after the master curve
        out[..., c] = np.interp(y[..., c], grid, t)
    return out


def _tone_s(x, s):
    """Tone curve with the shadows/highlights controls and the user's curves."""
    y = _tone(x, s.contrast)
    if s.shadows or s.highlights:
        y = np.clip(y, 0.0, 1.0)
        y = y + 1.4 * s.shadows * y * (1 - y) ** 3 + 1.4 * s.highlights * (1 - y) * y ** 3
    if s.curve:
        y = _apply_curves(y, s)
    return y


def to_scene(raw, s):
    """raw float32 HxWx3 (sensor linear) -> scene-linear float32, roughly 0..1."""
    t = raw / np.asarray(s.base, np.float32)
    if s.mono:
        # silver image is neutral: after removing the base tint all three channels carry the same
        # picture, so average them (less noise than any single channel)
        t = np.repeat(t.mean(axis=2, keepdims=True), 3, axis=2)
    return _scene_from_t(t, s)


def prepare(rgb, ir, s, ref_width=None, apply_geometry=True, crop=True):
    """Slow, settings-light half: geometry, holder detection, infrared healing.

    Returns (raw float32 HxWx3, holder mask or None, scale). Depends only on geometry, film base,
    film type and the dust settings, so the interactive preview can cache it."""
    if apply_geometry:
        rgb = orient(rgb, s)
        ir = orient(ir, s) if ir is not None else None
        if crop:
            rgb = crop_frac(rgb, s.crop)
            ir = crop_frac(ir, s.crop) if ir is not None else None
    raw = np.ascontiguousarray(rgb).astype(np.float32)
    scale = (ref_width or rgb.shape[1]) / 5184.0                   # sizes are tuned at 3600 dpi full width
    holder = holder_mask(raw, s.base) if s.negative and s.holder else None
    if ir is not None and s.dust and not s.mono:      # silver blocks infrared, so IR cleaning would eat the picture
        m = dust_mask(np.ascontiguousarray(ir), s.dust_threshold, s.dust_grow, max(0.15, scale))
        if holder is not None:
            m[holder] = 0
        raw = heal(raw, m, max(0.15, scale))
    return raw, holder, scale


def _finish(scene, holder, s, scale, out_bits, tone=None):
    if holder is not None:
        scene[holder] = 0.0                                        # film holder renders black, not white
    if s.saturation != 1.0 and not s.mono:
        lum = scene @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        scene = lum[..., None] + (scene - lum[..., None]) * np.float32(s.saturation)
    return _post(tone(scene) if tone else _tone_s(scene, s), s, scale, out_bits)


def _post(y, s, scale, out_bits):
    if s.grain > 0:
        y = np.clip(y, 0.0, 1.0).astype(np.float32)
        d = max(3, int(round(5 * scale))) | 1
        smooth = cv2.bilateralFilter(y, d, 0.10, max(1.0, 2.0 * scale))     # smooths grain, keeps edges
        y = y + min(1.0, s.grain) * (smooth - y)
    if s.sharpen > 0:
        y = np.clip(y, 0.0, 1.0).astype(np.float32)
        sigma = max(0.5, 1.6 * scale)                      # radius follows resolution so preview and scan agree
        y = y + s.sharpen * (y - cv2.GaussianBlur(y, (0, 0), sigma))
    if out_bits == 0:                                  # float 0..1, sRGB-encoded
        return np.clip(y, 0, 1).astype(np.float32)
    return quantise(y, out_bits)


def quantise(y, out_bits):
    if out_bits == 16:
        return (np.clip(y, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
    return (np.clip(y, 0, 1) * 255.0 + 0.5).astype(np.uint8)


def develop(rgb, ir=None, s=None, out_bits=8, apply_geometry=True, ref_width=None):
    """Full pipeline. rgb: uint16 HxWx3 raw scan; ir: uint16 HxW or None. Returns uint8/uint16 HxWx3."""
    s = s or Settings()
    raw, holder, scale = prepare(rgb, ir, s, ref_width, apply_geometry)
    return _finish(to_scene(raw, s), holder, s, scale, out_bits)


# -- fast path for the live preview ---------------------------------------------
# Same maths as develop(), evaluated through lookup tables on a cached, already-healed 16-bit image.

_TONE_N, _TONE_MAX = 8192, 16.0


def display_source(raw, holder, s, max_side=1100):
    """Shrink prepare() output for the screen and pack it as uint16 so tables can index it."""
    h, w = raw.shape[:2]
    f = min(1.0, max_side / max(h, w))
    if s.mono:                                   # fold the channel average in now: then each channel is 1-D again
        base = np.asarray(s.base, np.float32)
        raw = (raw / base).mean(axis=2, keepdims=True) * base
    if f < 1.0:
        raw = cv2.resize(raw, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA)
        if holder is not None:
            holder = cv2.resize(holder.astype(np.uint8), (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
    return np.clip(raw + 0.5, 0, 65535).astype(np.uint16), holder, f


def finish_display(src, holder, s, scale):
    """src: uint16 HxWx3 from display_source. Returns uint8 HxWx3. A few tens of milliseconds."""
    x = np.arange(65536, dtype=np.float32)[:, None] / np.asarray(s.base, np.float32)
    lut = _scene_from_t(x, s)                                           # 65536 x 3
    if s.mono or s.saturation == 1.0:            # no channel mixing: one table straight from sensor value to tone
        ylut = _tone_s(lut, s).astype(np.float32)
        y = np.empty(src.shape, np.float32)
        for c in range(3):
            y[..., c] = ylut[:, c][src[..., c]]
        if holder is not None:
            y[holder] = 0.0
        return _post(y, s, scale, 8)
    scene = np.empty(src.shape, np.float32)
    for c in range(3):
        scene[..., c] = lut[:, c][src[..., c]]
    grid = (np.arange(_TONE_N, dtype=np.float32) / (_TONE_N - 1)) ** 2 * _TONE_MAX      # dense near black
    table = _tone_s(np.repeat(grid[:, None], 3, axis=1), s).astype(np.float32)        # one column per channel

    def tone(v):
        idx = (np.sqrt(np.clip(v, 0.0, _TONE_MAX) / _TONE_MAX) * (_TONE_N - 1)).astype(np.int32)
        out = np.empty(v.shape, np.float32)
        for c in range(3):
            out[..., c] = table[:, c][idx[..., c]]
        return out

    return _finish(scene, holder, s, scale, 8, tone)


# -- automatic helpers (only run when the user asks) -----------------------------

def _holder_edge(p):
    """Index where film starts along a brightness profile running from the scan edge inwards.

    The holder is dark (with some flare), film is brighter: take the biggest upward step in the outer
    part of the profile, then walk on until the ramp has levelled off. 0 when there is no such step."""
    n = len(p)
    reach = max(4, int(n * 0.09))
    lp = np.log(np.maximum(p[:reach + 6], 1e-4))
    jump = lp[2:] - lp[:-2]
    i = int(np.argmax(jump[:reach]))
    if jump[i] < np.log(1.6):
        return 0
    j = i + 2
    while j < reach + 4 and p[j] < 0.93 * p[min(n - 1, j + 3)]:
        j += 1
    return j + max(1, int(n * 0.0015))


def holder_mask(raw, base, rect=True):
    """True where the scan shows the film holder, not film.

    rect=True: everything outside the rectangular holder opening, found from the brightness step at
    each side (straight, clean edges). rect=False: the raw outline of dark areas touching the scan
    edge, which is what straightening measures."""
    t_max = (raw / np.asarray(base, np.float32)).max(axis=2)
    h, w = t_max.shape
    if rect:
        prof_r = t_max[:, int(w * 0.2):int(w * 0.8)].mean(axis=1)
        prof_c = t_max[int(h * 0.2):int(h * 0.8), :].mean(axis=0)
        top, bottom = _holder_edge(prof_r), _holder_edge(prof_r[::-1])
        left, right = _holder_edge(prof_c), _holder_edge(prof_c[::-1])
        out = np.ones((h, w), bool)
        out[top:h - bottom, left:w - right] = False
        return out
    dark = (t_max < 0.25).astype(np.uint8)
    if not dark.any():
        return dark.astype(bool)
    n, labels = cv2.connectedComponents(dark, connectivity=4)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    edge = edge[edge != 0]
    if not len(edge):
        return np.zeros(dark.shape, bool)
    return np.isin(labels, edge)


def auto_exposure(rgb, s, target=0.85, pct=99.5):
    """Exposure (stops) that puts the `pct` percentile of the picture at `target` of white.

    Looks only at film (the holder is ignored) inside the crop, shrunk a little to skip the frame edge."""
    t = Settings.from_dict(s.to_dict()); t.exposure = 0.0; t.black = 0.0; t.white = 1.0
    view = crop_frac(orient(rgb, t), t.crop)
    h, w = view.shape[:2]
    view = view[int(h * 0.06):int(h * 0.94), int(w * 0.06):int(w * 0.94)]
    small = np.ascontiguousarray(view[::6, ::6]).astype(np.float32)
    keep = ~holder_mask(small, t.base)
    if keep.sum() < 100:
        return 0.0
    scene = to_scene(small, t)[keep]
    peak = float(np.percentile(scene.max(axis=1), pct))
    return float(np.clip(np.log2(target / max(peak, 1e-6)), -8, 8))


def neutral_wb_at(rgb, s, fx, fy, radius=0.008):
    """Density multipliers that make the clicked patch neutral (fractions of the displayed image)."""
    t = Settings.from_dict(s.to_dict())
    view = crop_frac(orient(rgb, t), t.crop)
    h, w = view.shape[:2]
    r = max(2, int(radius * w)); x, y = int(fx * w), int(fy * h)
    patch = view[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1].reshape(-1, 3).astype(np.float32)
    v = np.median(patch, axis=0)
    d = -np.log10(np.clip(v / np.asarray(t.base, np.float32), 1e-5, None))
    if np.any(d < 0.02):
        return t.wb
    k = d[1] / d
    return tuple(float(x) for x in k)


def restore_wb(rgb, s):
    """Fade restoration: density multipliers that give all three dye layers the same density range.

    Faded film loses dye unevenly, so one layer ends up flatter than the others. Returns `wb`,
    or None when a layer carries too little image to compare."""
    t = Settings.from_dict(s.to_dict())
    view = crop_frac(orient(rgb, t), t.crop)
    h, w = view.shape[:2]
    small = np.ascontiguousarray(view[int(h * .06):int(h * .94):4, int(w * .06):int(w * .94):4]).astype(np.float32)
    keep = ~holder_mask(small, t.base)
    if keep.sum() < 500:
        return None
    k = max(3, small.shape[1] // 100) | 1
    d = -np.log10(np.clip(cv2.blur(small, (k, k)) / np.asarray(t.base, np.float32), 1e-5, None))[keep]
    rng = np.percentile(d, 98, axis=0) - np.percentile(d, 5, axis=0)
    if np.any(rng < 0.05):
        return None
    return tuple(float(v) for v in np.clip(rng[1] / rng, 0.4, 2.5))


def auto_crop(rgb, s, inset=0.006):
    """Crop to the opening of the film holder. Returns (x0, y0, x1, y1) fractions of the oriented frame."""
    t = Settings.from_dict(s.to_dict())
    view = np.ascontiguousarray(orient(rgb, t)[::2, ::2]).astype(np.float32)
    film_px = ~holder_mask(view, t.base)
    cols = np.where(film_px.mean(axis=0) > 0.5)[0]
    rows = np.where(film_px.mean(axis=1) > 0.5)[0]
    if len(cols) < 10 or len(rows) < 10:
        return (0.0, 0.0, 1.0, 1.0)
    h, w = film_px.shape
    x0, x1, y0, y1 = cols[0] / w, (cols[-1] + 1) / w, rows[0] / h, (rows[-1] + 1) / h
    return (float(min(x0 + inset, 0.45)), float(min(y0 + inset, 0.45)),
            float(max(x1 - inset, 0.55)), float(max(y1 - inset, 0.55)))


def auto_angle(rgb, s):
    """Tilt of the holder opening in degrees (the correction to apply), from its long edges.

    0.0 when no clean edge is found."""
    t = Settings.from_dict(s.to_dict())
    t.angle = 0.0
    view = np.ascontiguousarray(orient(rgb, t)[::2, ::2]).astype(np.float32)
    film_px = ~holder_mask(view, t.base, rect=False)
    h, w = film_px.shape
    sign = 1.0
    if h > w:                                    # measure along the long side
        film_px = film_px.T
        h, w = w, h
        sign = -1.0
    xs = np.arange(int(w * 0.2), int(w * 0.8))
    slopes = []
    for edge in (np.argmax(film_px[:, xs], axis=0), h - 1 - np.argmax(film_px[::-1][:, xs], axis=0)):
        edge = edge.astype(np.float32)
        ok = np.abs(edge - np.median(edge)) < h * 0.03
        if ok.sum() > len(xs) * 0.6:
            slopes.append(np.polyfit(xs[ok], edge[ok], 1)[0])
    if not slopes:
        return 0.0
    ang = float(np.degrees(np.arctan(np.mean(slopes)))) * sign
    return round(-ang, 2) if abs(ang) < 6 else 0.0
