"""Keeps the TikTok and Instagram connections: app keys and tokens live in
the secure credential store (Windows Credential Manager), only names in the
settings file."""

from __future__ import annotations

import threading
from pathlib import Path

from core.social import instagram, tiktok
from core.social.errors import SocialAuthError

TIKTOK_APP, TIKTOK_TOKEN, INSTAGRAM = "social:tiktok-app", "social:tiktok-token", "social:instagram"


class SocialService:
    def __init__(self, config, store) -> None:
        self.config = config
        self.store = store
        self._lock = threading.Lock()

    # ------------------------------------------------------------ TikTok
    @property
    def tiktok_app(self) -> tiktok.TikTokApp | None:
        data = self.store.get(TIKTOK_APP) or {}
        if data.get("client_key") and data.get("client_secret"):
            return tiktok.TikTokApp(str(data["client_key"]), str(data["client_secret"]))
        return None

    def set_tiktok_app(self, client_key: str, client_secret: str) -> None:
        self.store.set(TIKTOK_APP, {"client_key": client_key.strip(), "client_secret": client_secret.strip()})

    @property
    def tiktok_name(self) -> str:
        """The connected TikTok account's name, or "" when not connected."""
        if self.store.get(TIKTOK_TOKEN) is None:
            return ""
        return str((self.config.social_accounts.get("tiktok") or {}).get("name", "")) or "TikTok account"

    def tiktok_connected(self, token: tiktok.TikTokToken, name: str) -> None:
        self.store.set(TIKTOK_TOKEN, token.to_dict())
        self.config.social_accounts = {**self.config.social_accounts, "tiktok": {"name": name,
                                                                                 "open_id": token.open_id}}

    def tiktok_access_token(self) -> str:
        """A valid access token, refreshed when needed (call off the GUI thread)."""
        with self._lock:
            data = self.store.get(TIKTOK_TOKEN)
            app = self.tiktok_app
            if data is None or app is None:
                raise SocialAuthError("TikTok is not connected. Open the TikTok guide and connect.")
            token = tiktok.TikTokToken.from_dict(data)
            if token.expired:
                token = tiktok.refresh(app, token)
                self.store.set(TIKTOK_TOKEN, token.to_dict())
            return token.access_token

    def disconnect_tiktok(self) -> None:
        data, app = self.store.get(TIKTOK_TOKEN), self.tiktok_app
        self.store.delete(TIKTOK_TOKEN)
        self.config.social_accounts = {k: v for k, v in self.config.social_accounts.items() if k != "tiktok"}
        if data and app:
            threading.Thread(target=tiktok.revoke, args=(app, tiktok.TikTokToken.from_dict(data)),
                             daemon=True).start()

    def post_tiktok(self, path: Path, post: tiktok.TikTokPost, progress=None, cancel=None) -> tiktok.TikTokResult:
        token = self.tiktok_access_token()
        username = str((self.config.social_accounts.get("tiktok") or {}).get("username", ""))
        return tiktok.post_video(token, path, post, progress, cancel, username=username)

    # ------------------------------------------------------------ Instagram
    @property
    def instagram_account(self) -> instagram.InstagramAccount | None:
        data = self.store.get(INSTAGRAM)
        try:
            return instagram.InstagramAccount.from_dict(data) if data else None
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def instagram_name(self) -> str:
        account = self.instagram_account
        return f"@{account.username}" if account and account.username else ("Instagram account" if account else "")

    def instagram_connected(self, account: instagram.InstagramAccount) -> None:
        self.store.set(INSTAGRAM, account.to_dict())
        self.config.social_accounts = {**self.config.social_accounts,
                                       "instagram": {"name": account.username, "user_id": account.user_id}}

    def disconnect_instagram(self) -> None:
        self.store.delete(INSTAGRAM)
        self.config.social_accounts = {k: v for k, v in self.config.social_accounts.items() if k != "instagram"}

    def post_reel(self, path: Path, caption: str, share_to_feed: bool = True, cover_ms: int | None = None,
                  progress=None, cancel=None) -> instagram.ReelResult:
        with self._lock:
            account = self.instagram_account
            if account is None:
                raise SocialAuthError("Instagram is not connected. Open the Instagram guide and connect.")
            fresh = instagram.refresh(account)
            if fresh is not account:
                self.store.set(INSTAGRAM, fresh.to_dict())
        return instagram.post_reel(fresh, path, caption, share_to_feed, cover_ms, progress, cancel)
