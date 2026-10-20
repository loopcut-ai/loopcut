"""Video facts for motion_from_video without Blender: the container type and the clip length.

MP4 and MOV are ISO base media files: the movie header box (moov/mvhd) holds a timescale and a duration,
read here by walking the box tree and seeking over the media data, so a 200 MB clip costs a few small
reads. WebM's length is in Matroska's Segment Info, which recorders often leave out; ffprobe is asked when
it is installed, otherwise the caller must give the length.
"""

import json
import shutil
import struct
import subprocess

FFPROBE_TIMEOUT = 20
_ISO_BRANDS_QT = {b"qt  "}
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}


class MediaError(ValueError):
    pass


def sniff_video(head):
    """video/mp4, video/quicktime or video/webm from a file's first 64 bytes, else None."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:12] in _ISO_BRANDS_QT else "video/mp4"
    if len(head) >= 8 and head[4:8] in {b"moov", b"wide", b"mdat", b"free", b"skip"}:
        return "video/quicktime"  # Old QuickTime files start without an ftyp box.
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in head[:64] else "video/x-matroska"
    return None


def _boxes(handle, start, end):
    """(type, payload offset, payload end) for each box between start and end."""
    offset = start
    while offset + 8 <= end:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        payload = offset + 8
        if size == 1:
            large = handle.read(8)
            if len(large) < 8:
                return
            size = struct.unpack(">Q", large)[0]
            payload += 8
        elif size == 0:
            size = end - offset  # The box runs to the end of the file.
        if size < payload - offset or offset + size > end:
            return
        yield kind, payload, offset + size
        offset += size


def _header_time(handle, start, offset_v0, offset_v1):
    """(timescale, duration) of an mvhd or mdhd box whose payload starts at start."""
    handle.seek(start)
    version = handle.read(4)[:1]
    if version == b"\x01":
        data = handle.read(offset_v1 + 12)
        if len(data) < offset_v1 + 12:
            return None
        timescale, duration = struct.unpack(">IQ", data[offset_v1:offset_v1 + 12])
    else:
        data = handle.read(offset_v0 + 8)
        if len(data) < offset_v0 + 8:
            return None
        timescale, duration = struct.unpack(">II", data[offset_v0:offset_v0 + 8])
    if not timescale or duration in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return None
    return duration / timescale


def _video_track_seconds(handle, start, end):
    """The media duration of a trak whose handler is 'vide', else None."""
    for kind, payload, box_end in _boxes(handle, start, end):
        if kind != b"mdia":
            continue
        handler = seconds = None
        for inner, inner_start, _inner_end in _boxes(handle, payload, box_end):
            if inner == b"hdlr":
                handle.seek(inner_start + 8)
                handler = handle.read(4)
            elif inner == b"mdhd":
                seconds = _header_time(handle, inner_start, 8, 16)
        return seconds if handler == b"vide" else None
    return None


def iso_seconds(path):
    """The length of an MP4 or MOV file in seconds: its longest video track (mdhd, what decoders and the
    motion worker see), else the movie header (mvhd); None when it has neither."""
    with open(path, "rb") as handle:
        end = handle.seek(0, 2)
        for kind, payload, box_end in _boxes(handle, 0, end):
            if kind != b"moov":
                continue
            movie, tracks = None, []
            for inner, start, inner_end in _boxes(handle, payload, box_end):
                if inner == b"mvhd":
                    movie = _header_time(handle, start, 8, 16)
                elif inner == b"trak":
                    seconds = _video_track_seconds(handle, start, inner_end)
                    if seconds:
                        tracks.append(seconds)
            return max(tracks) if tracks else movie
    return None


def ffprobe_seconds(path):
    """The container duration from ffprobe, or None when ffprobe is missing or can't tell."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                                   capture_output=True, stdin=subprocess.DEVNULL, timeout=FFPROBE_TIMEOUT)
        value = float(json.loads(completed.stdout or b"{}").get("format", {}).get("duration"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    return value if value > 0 else None


def video_seconds(path, mime):
    """The clip length in seconds: MP4/MOV from their header, WebM through ffprobe. None when unknown."""
    if mime in {"video/mp4", "video/quicktime"}:
        try:
            seconds = iso_seconds(path)
        except OSError as ex:
            raise MediaError(f"Could not read {path}: {ex.strerror or ex}") from None
        return seconds if seconds is not None else ffprobe_seconds(path)
    return ffprobe_seconds(path)
