# Film Scan

A film scanning app for the **Plustek OpticFilm 8200i** on Windows. It scans, inverts colour negatives, removes dust with the infrared channel, and lets you grade the result, all in one window.

It talks to the scanner through Plustek's own driver, so it needs no other scanning software.

## Requirements

- Windows 10 or 11 (64-bit)
- Plustek OpticFilm 8200i with the Plustek driver installed (the default location, `C:\Program Files (x86)\Plustek\OpticFilm 8200i`)
- Python 3.10 or newer

## Install and run

```
git clone https://github.com/Bardia323/filmscan.git
cd filmscan
setup.bat
```

Then double-click `FilmScan.exe`. Close any other scanning program first; only one can hold the scanner.

## How to use

1. **Preview.** The crop box fits itself to the frame; adjust the edges if needed.
2. **Scan.** The file is saved cropped to the box, in the output folder.
3. Adjust tone and colour on the scan, then **Save over scan**, or **Save as version** to keep the original and write `name_v2`, `_v3` ... beside it. Draw a second crop and **Save cropped copy** for a tighter version.

Scroll to zoom, drag to pan, Ctrl+Z to undo.

## Features

- Colour negative, black and white negative, and slide film
- Film base measured once and held for the roll, so colour does not drift between frames
- Preview and saved file use the same processing, so they match
- Infrared dust removal, sharpening and grain reduction (Off / Light / Medium / Heavy)
- Exposure, contrast, black and white point, shadows, highlights, shadow colour, curves (all channels and per channel)
- Scan is matched to the preview it was graded on; undo history survives scans; earlier film bases can be recalled
- Multi-pass averaging and multi-exposure for cleaner dense areas
- 900 to 7200 dpi; 48-bit TIFF, 24-bit TIFF or JPEG; sRGB, Adobe RGB or ProPhoto
- Print output: any CMYK or print RGB profile installed in Windows (ISO Coated v2 / FOGRA39, PSO, SWOP, eciRGB ...), or dropped into a `profiles` folder; saved 8-bit with the profile embedded
- Presets, including Cinematic and Renaissance painting looks
- Optional raw file per scan, which can be reopened and developed again

## Known limits

- Developed and tested on one scanner. Other Plustek models that use the same driver may work, but are untested.
- The scanner goes to sleep about 5 minutes after a scan and has to be woken with its button. Not solved yet.
- Hardware exposure and multi-exposure need 900 dpi or more.
- Infrared cleaning removes dust and hairs, not fine scratches. It is switched off for black and white film, which blocks infrared.
- 7200 dpi and slide film have had little testing.

## How it works

`plustek.py` loads Plustek's driver (`Scndrvu_x64.drv`) and calls its scan functions directly. The calling convention was worked out by inspecting the driver; see [docs/driver-notes.md](docs/driver-notes.md). `film.py` does the colour processing, and `server.py` serves the interface in `static/` to a local window.

No Plustek code is included. This project is not affiliated with Plustek.

## License

MIT
