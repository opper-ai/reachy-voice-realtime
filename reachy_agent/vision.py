"""Reachy camera → image.input events for the realtime model.

snapshot()  — capture+encode the latest frame; returns the data-URL string.
              Also caches the JPEG bytes so the UI can serve the same image.
"""

from __future__ import annotations

import base64
import io
import threading
from typing import Optional

import numpy as np
from PIL import Image


class Eyes:
    def __init__(self, mini, jpeg_quality: int = 70):
        self._mini = mini
        self._quality = jpeg_quality
        self._lock = threading.Lock()
        self._last_jpeg: Optional[bytes] = None
        self._last_data_url: Optional[str] = None

    def snapshot(self) -> Optional[str]:
        """Grab a frame, encode JPEG, return a base64 data URL.

        Returns None if no frame is available yet.
        """
        frame = self._mini.media.get_frame()
        if frame is None:
            return None
        # get_frame returns BGR uint8; PIL wants RGB.
        if frame.ndim == 3 and frame.shape[2] == 3:
            rgb = frame[:, :, ::-1]
        else:
            rgb = frame
        img = Image.fromarray(np.ascontiguousarray(rgb))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._quality)
        jpeg = buf.getvalue()
        b64 = base64.b64encode(jpeg).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"
        with self._lock:
            self._last_jpeg = jpeg
            self._last_data_url = data_url
        return data_url

    @property
    def last_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._last_jpeg
