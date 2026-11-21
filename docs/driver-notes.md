# Driver notes: Plustek OpticFilm 8200i (ScanAPI)

What is known about calling Plustek's Windows driver directly. Worked out by inspecting the installed driver and
observing its behaviour on one scanner (USB `07B3:1825`, module "A2F"). Offsets are for `Scndrvu_x64.drv` dated
August 2025 and may differ in other versions. Nothing here is official.

## Loading

- File: `C:\Program Files (x86)\Plustek\OpticFilm 8200i\Scndrvu_x64.drv` (a DLL).
- Set the current directory to that folder while loading and registering; the driver reads its `.ini` files from there.
- Export `SCANAPI_GetFunctionTable()` returns a pointer to a table of function pointers (8 bytes each).
- All functions return `int`: 0 = success, negative = error. Some errors pop a message box, so run experiments with a timeout.

| Slot | Function | Arguments |
|---|---|---|
| 0 | Register | `WORD version[4]`, use `{1,0,0,4}` |
| 1 | Unregister | none |
| 3 | GetDeviceCapability | `void **out`: table of pointers, index = source id |
| 4 | GetDeviceStatus | `int source, WORD *status` (set `status[0] = 1`); returns 0 while the scanner answers |
| 5 | GetImageLayout | `ScanProperty *, DWORD out[]`: pixels per line, bytes per line, lines |
| 6 | StartScan | `ScanParam *` (calibrates first, about 8 s) |
| 7 | Read | `void *buf, DWORD bytes, DWORD *lines_read` |
| 8 | StopScan | none |
| 14 | LampControl | `int source, int status` |
| 20 | LampOffTime | `int *minutes, int set, int type` (type 0 = lamp off, 1 = standby) |
| 39 | EndScan | `int` (pass 0) |

Error codes seen: `-90` scanner not on USB, `-94` not scanning / bad state, `-86` busy.

## Structures (packed, little-endian)

`ScanProperty`, 0x3F bytes:

| Offset | Type | Field |
|---|---|---|
| 0x00 | int32 | source (1 = film) |
| 0x04 | int32 | lens (0) |
| 0x08 | int32 | mode (1 = grey, 2 = colour) |
| 0x0C | uint16 | bits per channel (16) |
| 0x0E | uint32 | dpi X |
| 0x12 | uint32 | dpi Y |
| 0x16 | double x4 | origin x, origin y, width, height |
| 0x36 | uint32 | unit (3 = pixels) |

`ScanParam`, 0x51 bytes:

| Offset | Type | Field |
|---|---|---|
| 0x00 | pointer | `ScanProperty *` |
| 0x08 | uint32 | flags |
| 0x14 | pointer | event callback `int cb(int code, int value)`; always supply one |
| 0x3B | uint32 | bit 0: use the exposure level below |
| 0x3F | uint8 | exposure level 2, 3 or 4 (multiples of the normal exposure) |

Everything else zero.

Flags: `0x00010000` the scan has an infrared page; `0x10000000` this StartScan reads the infrared page.

Callback events seen: 2 = calibration begins, 3 = calibration ends.

## Scan sequence

```
Register
GetImageLayout(prop)            -> pixels per line, bytes per line, lines
StartScan(param)
loop: Read(buf, bytes, &lines)  # the length is in bytes, the count returned is in LINES
StopScan
EndScan(0)
Unregister
```

- Scan area for film is 1.44 x 1.00 inch; any resolution from 300 to 7200 dpi is accepted.
- Data is 16-bit linear RGB, unprocessed.

### Infrared

Two StartScan calls for the same area:

1. flags `0x00010000`: colour. `Read` returns 6 at the end of the page.
2. flags `0x10010000`: infrared, returned in all three channels. Clear film is near white; dust and scratches are dark.

Slot 33 (`ReadDuplex`) is not the infrared path.

### Exposure

Set `+0x3B = 1` and `+0x3F = level`. Works only at 900 dpi and above; below that `Read` fails with -94 and the connection
has to be re-registered. Level 2 gives 2.0x the light, level 4 gives 4.0x, and the pass takes proportionally longer.
Separate passes of the same area align within about 0.15 pixel, so a normal pass and a long pass can be merged.

## Sleep

The scanner drops off USB about 5 minutes after a scan and needs its power button to come back. Setting standby to 0 and
lamp-off to 60 through slot 20 is accepted and reads back, but does not prevent this. Unresolved.

## Colour notes

- Measure the film base on a smoothed image. A high percentile of raw pixels lands about three noise sigmas too bright
  and tints the shadows.
- The film holder is not black in the raw data (flare gives it 5 to 20 percent of film-base level), so find it by the
  brightness step at each edge, not by a darkness threshold.

## Tools

In `tools/` (need `requirements-dev.txt`): `pedis.py` disassembles a function with string and import annotations,
`functable.py` lists the function table with the debug strings each function references, `xref.py` finds references to
an address range, `capture_vuescan.py` records the driver calls another program makes.
