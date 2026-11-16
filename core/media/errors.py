"""Exceptions raised by the media layer."""


class MediaError(Exception):
    """Base class for media problems that can be shown to the user."""


class FFmpegNotFoundError(MediaError):
    """FFmpeg or FFprobe could not be located."""


class ProbeError(MediaError):
    """FFprobe failed or returned data that cannot be used."""
