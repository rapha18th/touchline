"""Frames on disk: one JPEG per frame, read one at a time to keep memory flat."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


class Clip:
    """A filmed clip as the pipeline sees it: frames plus clip.json (size, rate and kit colours)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.info = json.loads((self.path / "clip.json").read_text(encoding="utf-8"))
        self.files = sorted((self.path / "frames").glob("*.jpg"))
        self.id = self.info["id"]
        self.fps = float(self.info["fps"])
        self.w, self.h = int(self.info["w"]), int(self.info["h"])

    def __len__(self) -> int:
        return len(self.files)

    def frame(self, i: int) -> np.ndarray:
        return cv2.imread(str(self.files[i]), cv2.IMREAD_COLOR)

    def background(self, n: int = 31) -> np.ndarray:
        """Per-pixel median of n frames spread over the clip: the pitch with the moving players removed.
        The camera is fixed for a clip, so this is the empty pitch."""
        idx = np.unique(np.linspace(0, len(self) - 1, num=min(n, len(self))).round().astype(int))
        stack = np.stack([self.frame(int(i)) for i in idx])
        bg = np.median(stack, axis=0).astype(np.uint8)
        del stack
        return bg
