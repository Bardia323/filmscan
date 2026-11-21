"""Output colour spaces: conversion from the pipeline's sRGB-encoded result, and ICC profiles to embed."""
import glob
import os
import struct

import numpy as np

# RGB -> XYZ matrices, colorants adapted to D50 (as stored in ICC profiles)
_M = {
    "srgb": np.array([[0.43607, 0.38515, 0.14307], [0.22249, 0.71687, 0.06061], [0.01392, 0.09708, 0.71410]]),
    "adobe": np.array([[0.60974, 0.20528, 0.14919], [0.31111, 0.62567, 0.06322], [0.01947, 0.06087, 0.74457]]),
    "prophoto": np.array([[0.79767, 0.13519, 0.03134], [0.28804, 0.71188, 0.00009], [0.00000, 0.00000, 0.82521]]),
}
_GAMMA = {"adobe": 563 / 256, "prophoto": 1.8}
NAMES = {"srgb": "sRGB", "adobe": "Adobe RGB (1998)", "prophoto": "ProPhoto RGB"}


def convert(y, space):
    """y: float32 HxWx3 sRGB-encoded 0..1 -> same picture encoded in `space`."""
    if space == "srgb" or space not in _M:
        return y
    y = np.clip(y, 0.0, 1.0)
    lin = np.where(y <= 0.04045, y / 12.92, ((y + 0.055) / 1.055) ** 2.4)
    m = (np.linalg.inv(_M[space]) @ _M["srgb"]).astype(np.float32)
    out = np.clip(lin.astype(np.float32) @ m.T, 0.0, 1.0)
    return np.power(out, 1.0 / _GAMMA[space]).astype(np.float32)


def _s15(v):
    return struct.pack(">i", int(round(v * 65536)))


def _xyz(v):
    return b"XYZ \0\0\0\0" + b"".join(_s15(x) for x in v)


def _text(tag, s):
    b = s.encode("ascii") + b"\0"
    if tag == "desc":
        return b"desc\0\0\0\0" + struct.pack(">I", len(b)) + b + b"\0" * 78
    return b"text\0\0\0\0" + b


def icc(space):
    """ICC profile bytes for `space` (matrix/TRC, version 2)."""
    if space == "srgb" or space not in _M:
        from PIL import ImageCms
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    m, g = _M[space], _GAMMA[space]
    curve = b"curv\0\0\0\0" + struct.pack(">IH", 1, int(round(g * 256))) + b"\0\0"
    tags = [(b"desc", _text("desc", NAMES[space])), (b"cprt", _text("text", "No copyright, use freely")),
            (b"wtpt", _xyz((0.9642, 1.0, 0.8249))),
            (b"rXYZ", _xyz(m[:, 0])), (b"gXYZ", _xyz(m[:, 1])), (b"bXYZ", _xyz(m[:, 2])),
            (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]
    table, data, off = b"", b"", 128 + 4 + 12 * len(tags)
    for sig, body in tags:
        body += b"\0" * (-len(body) % 4)
        table += sig + struct.pack(">II", off + len(data), len(body))
        data += body
    size = 128 + 4 + len(table) + len(data)
    head = struct.pack(">I4sI4s4s4s", size, b"\0\0\0\0", 0x02100000, b"mntr", b"RGB ", b"XYZ ")
    head += struct.pack(">6H", 2024, 1, 1, 0, 0, 0) + b"acsp" + b"MSFT" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8
    head += struct.pack(">I", 0) + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\0" * 4
    head += b"\0" * (128 - len(head))
    return head + struct.pack(">I", len(tags)) + table + data


# -- installed ICC profiles (print standards: ISO Coated / FOGRA, PSO, SWOP, GRACoL, eciRGB ...) ---------------------
# These are not shipped with the app. It offers whatever is installed in Windows' colour folder, plus any profile
# dropped into a "profiles" folder next to the app. Conversion is done by LittleCMS (through Pillow), from sRGB.

PROFILE_DIRS = [os.path.join(os.environ.get("WINDIR", "C:/Windows"), "System32", "spool", "drivers", "color"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")]
_RGB_PRINT_NAMES = ("ecirgb",)                 # RGB working spaces worth listing besides the built-in three
_found = None


def installed_profiles(refresh=False):
    """[{id, name, kind, path}] for CMYK print profiles and print-oriented RGB spaces found on this machine."""
    global _found
    if _found is not None and not refresh:
        return _found
    from PIL import ImageCms
    out, seen = [], set()
    for d in PROFILE_DIRS:
        own = os.path.basename(d) == "profiles"
        for path in sorted(glob.glob(os.path.join(d, "*.ic[cm]"))):
            try:
                p = ImageCms.getOpenProfile(path)
                kind = p.profile.xcolor_space.strip()
                name = ImageCms.getProfileDescription(p).strip() or os.path.basename(path)
            except Exception:
                continue
            if kind == "CMYK" or (kind == "RGB" and (own or any(k in name.lower() for k in _RGB_PRINT_NAMES))):
                if name in seen:
                    continue
                seen.add(name)
                out.append(dict(id="icc:" + os.path.basename(path), name=name, kind=kind, path=path))
    _found = sorted(out, key=lambda e: (e["kind"] != "RGB", e["name"].lower()))
    return _found


def find_profile(space):
    """The installed profile for a colour-space id like 'icc:ISOcoated_v2_eci.icc', or None."""
    if not isinstance(space, str) or not space.startswith("icc:"):
        return None
    for e in installed_profiles():
        if e["id"] == space:
            return e
    return None


def convert_with_profile(y, entry):
    """y: float32 HxWx3, sRGB-encoded 0..1 -> (uint8 array in the profile's space, mode 'CMYK' or 'RGB', profile bytes).

    Relative colorimetric with black point compensation, the usual choice for photographs going to print.
    8 bits per channel: that is what the conversion engine offers here."""
    from PIL import Image, ImageCms
    src = Image.fromarray((np.clip(y, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGB")
    dst_profile = ImageCms.getOpenProfile(entry["path"])
    mode = "CMYK" if entry["kind"] == "CMYK" else "RGB"
    t = ImageCms.buildTransform(ImageCms.createProfile("sRGB"), dst_profile, "RGB", mode,
                                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                                flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
    out = ImageCms.applyTransform(src, t)
    with open(entry["path"], "rb") as f:
        return np.asarray(out), mode, f.read()
