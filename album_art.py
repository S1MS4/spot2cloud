"""Album cover art as a file in the album folder.

Tags carry embedded art, but a folder-level ``cover.jpg`` is what music
players, file browsers and the SoundCloud uploader can actually point at. This
module writes exactly one per album folder, and is deliberately best-effort:
a download must never fail because artwork could not be saved.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path
from typing import Optional

__all__ = ["COVER_NAME", "save_folder_cover", "embedded_cover_bytes"]

logger = logging.getLogger(__name__)

COVER_NAME = "cover.jpg"

_FETCH_TIMEOUT = 15


def embedded_cover_bytes(path: Path) -> Optional[bytes]:
    """Pull cover art out of a tagged audio file, or return None.

    Handles the two shapes this pipeline produces: ID3 ``APIC`` for MP3 and a
    base64 ``METADATA_BLOCK_PICTURE`` for Opus/Ogg.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            for frame in ID3(str(path)).getall("APIC"):
                if frame.data:
                    return bytes(frame.data)
            return None

        if suffix in (".opus", ".ogg"):
            import base64

            from mutagen.flac import Picture
            from mutagen.oggopus import OggOpus

            blocks = OggOpus(str(path)).get("metadata_block_picture") or []
            for block in blocks:
                data = Picture(base64.b64decode(block)).data
                if data:
                    return bytes(data)
            return None
    except Exception:  # noqa: BLE001 - artwork is never worth failing over
        logger.debug("could not read embedded art from %s", path, exc_info=True)
    return None


def _to_jpeg(raw: bytes) -> Optional[bytes]:
    """Re-encode arbitrary image bytes as JPEG, or None if unreadable.

    YouTube thumbnails arrive as PNG or WebP; SoundCloud and most players want
    JPEG. Passes JPEG through untouched rather than re-encoding it, which would
    cost a generation of quality for nothing.
    """
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(raw))
        if (image.format or "").upper() == "JPEG":
            return raw
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=92)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        logger.debug("could not convert cover art to JPEG", exc_info=True)
        return None


def save_folder_cover(
    folder: Path,
    cover_url: Optional[str] = None,
    fallback_track: Optional[Path] = None,
    overwrite: bool = False,
) -> Optional[Path]:
    """Write ``cover.jpg`` into ``folder`` and return its path.

    Prefers ``cover_url`` (the square release art), falling back to whatever is
    embedded in ``fallback_track`` -- for a YouTube-sourced album that is the
    video thumbnail, which is 16:9 but better than nothing.

    Returns None when no art could be obtained. Never raises.
    """
    folder = Path(folder)
    target = folder / COVER_NAME
    if target.exists() and not overwrite:
        return target

    raw = None
    if cover_url:
        try:
            request = urllib.request.Request(
                cover_url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:
                raw = response.read()
        except Exception:  # noqa: BLE001
            logger.debug("could not fetch cover from %s", cover_url, exc_info=True)

    if not raw and fallback_track is not None:
        raw = embedded_cover_bytes(Path(fallback_track))

    if not raw:
        return None

    jpeg = _to_jpeg(raw)
    if not jpeg:
        return None

    try:
        folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jpeg)
    except OSError:
        logger.warning("could not write %s", target, exc_info=True)
        return None

    logger.debug("wrote %s (%d bytes)", target, len(jpeg))
    return target
