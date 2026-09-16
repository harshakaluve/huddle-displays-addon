"""Layout constants and font loading for the 800x480 1-bit panel.

Everything here is in device pixels. The panel is 800x480 monochrome, so there
is exactly one ink colour: 0 (black) on 255 (white). No greys -- any grey has to
be built out of a dither pattern, see render.shade().
"""
from __future__ import annotations

import os
from functools import lru_cache

from PIL import ImageFont

W, H = 800, 480

# --- vertical bands -------------------------------------------------------
HEADER_H = 76           # black band: logo, room name, date, last refresh
TIMELINE_H = 112        # bottom strip: proportional day timeline
BODY_TOP = HEADER_H
BODY_BOTTOM = H - TIMELINE_H      # 368
TIMELINE_TOP = BODY_BOTTOM

# --- horizontal rhythm ----------------------------------------------------
PAD = 30                # outer margin, left/right
CONTENT_L = PAD
CONTENT_R = W - PAD     # 770

BLACK = 0
WHITE = 255

# Antialiased text is thresholded to pure black/white at this cut-off.
# Lower = fatter glyphs. 150 keeps 13px labels legible without blobbing 76px text.
TEXT_THRESHOLD = 150

# Font candidates in preference order. Inter is what the add-on image installs;
# the rest are fallbacks so the renderer still runs on a bare machine.
_FAMILIES = {
    "black":    ["Inter-Black.otf", "Inter-ExtraBold.otf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "bold":     ["Inter-Bold.otf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "semibold": ["Inter-SemiBold.otf", "Inter-Bold.otf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "medium":   ["Inter-Medium.otf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
    "regular":  ["Inter-Regular.otf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"],
}

_SEARCH_DIRS = [
    os.path.join(os.path.dirname(__file__), "assets", "fonts"),
    "/usr/share/fonts/opentype/inter",
    "/usr/share/fonts/truetype/inter",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/dejavu",
]


@lru_cache(maxsize=256)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for filename in _FAMILIES[weight]:
        for directory in _SEARCH_DIRS:
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    raise RuntimeError(
        f"No font found for weight {weight!r}. Install fonts-inter, or drop the "
        f"Inter .otf files into app/assets/fonts/."
    )
