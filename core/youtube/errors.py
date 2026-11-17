"""Errors raised by the YouTube integration."""

from __future__ import annotations


class YouTubeError(RuntimeError):
    """Any failure talking to Google / YouTube, with a user-readable message."""

    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class YouTubeAuthError(YouTubeError):
    """Sign-in is missing, expired or was revoked: the user must reconnect."""


class YouTubeQuotaError(YouTubeError):
    """The daily API quota or the upload limit is used up."""


class AuthCancelled(Exception):
    pass


class UploadCancelled(Exception):
    pass
