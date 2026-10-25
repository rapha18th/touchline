"""The grass and the painted lines, found in an empty-pitch image."""

from __future__ import annotations

import cv2
import numpy as np


def grass_mask(bgr: np.ndarray) -> np.ndarray:
    """Turf and its margin: pixels where green clearly leads red and blue. Largest region, holes filled."""
    b, g, r = (bgr[..., i].astype(np.int16) for i in range(3))
    m = ((g - np.maximum(r, b) > 10) & (g > 28)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return m
    keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    m = (lab == keep).astype(np.uint8)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(m)
    cv2.drawContours(out, cnts, -1, 1, thickness=-1)
    return out


def line_mask(bgr: np.ndarray, grass: np.ndarray | None = None, thresh: int = 16) -> np.ndarray:
    """Thin bright marks on the grass: a white top-hat on luminance, kept where the pixel is pale."""
    grass = grass_mask(bgr) if grass is None else grass
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    pale = hsv[..., 1] < 150
    inside = cv2.erode(grass, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))) > 0
    m = ((th > thresh) & pale & inside).astype(np.uint8)
    # Drop specks: painted lines are long.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    big = np.zeros(n, bool)
    big[1:] = np.maximum(stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_HEIGHT]) >= 12
    return big[lab].astype(np.uint8)
