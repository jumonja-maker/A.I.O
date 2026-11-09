# Offline ICC controls for color-model conversion regressions

These are test-only color profiles, never application runtime payloads.
Tests synthesize small JPEG/PNG images locally; no download or external
conversion engine runs during the source suite.

* `CGATS001Compat-v2-micro.icc` (8464 bytes, SHA-256
  `73e1ba37d2bad5bab2a964f40a9eed96209666efc067c3322626214bbef234a0`)
  was copied unchanged from the installed `pdfjs-dist` 5.6.205 `iccs` distribution.
  Upstream: https://github.com/saucecontrol/Compact-ICC-Profiles .
  The accompanying distribution license is retained as
  `LICENSE-CMYK-CC0.txt` (CC0 1.0 Universal).
* `Gray.icc` (420 bytes, SHA-256
  `4704d77882a57dc9631d300a75c33951293cb938984abef6077f0b5bef8dfb1e`)
  was copied unchanged from the installed Debian `icc-profiles-free` package,
  `/usr/share/color/icc/Gray.icc`. Its complete source copyright record is
  `SOURCE-Debian-copyright.txt`; the `Files: LCMS* compat* Gray* ...` stanza
  assigns the zlib/libpng license and credits the original profile authors.

The independent color oracle is Pillow's existing LittleCMS binding with
relative-colorimetric rendering intent, matching Qt's color transform intent.
Lossless output samples allow at most two 8-bit channel levels of difference
between Qt and LittleCMS (LUT interpolation/rounding); they must retain a valid
RGB ICC profile. This is a control over eight known decoded source samples,
not a general perceptual color-equivalence certificate.
