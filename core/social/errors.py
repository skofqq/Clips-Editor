"""Errors of the TikTok / Instagram integrations, with user-readable messages."""

from __future__ import annotations


class SocialError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class SocialAuthError(SocialError):
    """The sign-in is missing, expired or revoked: connect again."""


class PostCancelled(Exception):
    pass
