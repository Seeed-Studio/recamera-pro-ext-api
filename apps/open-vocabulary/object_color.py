"""Color-prefixed queries and conservative RGB crop classification."""
import numpy as np


COLORS = ("red", "orange", "yellow", "green", "blue", "purple", "pink",
          "brown", "black", "white", "gray")


def split_color_query(prompt):
    """Interpret one supported color followed by an object name."""
    color, _, noun = prompt.partition(" ")
    color = "gray" if color == "grey" else color
    if color in COLORS and noun.strip():
        return " ".join(noun.split()), color
    return prompt, None


def dominant_color(rgb):
    """Classify sampled interior pixels, returning None for ambiguous crops."""
    if rgb.ndim != 3 or rgb.shape[-1] != 3 or rgb.shape[0] * rgb.shape[1] < 32:
        return None
    red, green, blue = rgb.reshape(-1, 3).astype(np.float32).T / 255.0
    value = np.maximum(np.maximum(red, green), blue)
    chroma = value - np.minimum(np.minimum(red, green), blue)
    saturation = chroma / np.maximum(value, 1e-6)
    hue = (60 * np.where(value == red, green - blue,
                         np.where(value == green, blue - red, red - green))
           / np.maximum(chroma, 1e-6)
           + np.where(value == red, 0, np.where(value == green, 120, 240))) % 360

    colorful = (saturation >= .28) & (chroma >= .13) & (value >= .22)
    neutral = saturation <= .20
    warm = (hue >= 15) & (hue < 43)
    masks = (
        colorful & ((hue < 15) | (hue >= 348)),
        colorful & warm & (value >= .65),
        colorful & (hue >= 43) & (hue < 75),
        colorful & (hue >= 75) & (hue < 165),
        colorful & (hue >= 185) & (hue < 265),
        colorful & (hue >= 265) & (hue < 312),
        colorful & (hue >= 312) & (hue < 348) & (value >= .56),
        colorful & warm & (value < .56),
        (value < .20) & (chroma < .13),
        neutral & (value >= .65),
        neutral & (value >= .24) & (value < .57),
    )
    counts = [int(np.count_nonzero(mask)) for mask in masks]
    winner = int(np.argmax(counts))
    runner_up = max(count for index, count in enumerate(counts) if index != winner)
    if counts[winner] < .55 * value.size or counts[winner] - runner_up < .15 * value.size:
        return None
    return COLORS[winner]


def box_color(frame, box, crop_roi):
    """Sample the interior of an original-frame box via Kit's RGB ROI path."""
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    if min(width, height) < 10:
        return None
    interior = (x1 + .18 * width, y1 + .18 * height,
                x2 - .18 * width, y2 - .18 * height)
    roi, (left, top, sx, sy) = crop_roi(frame, interior, 48, pad=0)
    # Kit returns uniform gray 114 when a hardware crop fails. An absent crop
    # must never count as gray evidence. Remove the square crop's side margins.
    if np.all(roi == 114) or sx <= 0 or sy <= 0:
        return None
    x_start, x_stop = (int(np.clip(round((x - left) / sx), 0, roi.shape[1]))
                       for x in (interior[0], interior[2]))
    y_start, y_stop = (int(np.clip(round((y - top) / sy), 0, roi.shape[0]))
                       for y in (interior[1], interior[3]))
    return dominant_color(roi[y_start:y_stop, x_start:x_stop])
