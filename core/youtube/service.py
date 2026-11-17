"""Connected YouTube accounts: sign-in, token refresh, disconnect.

Non-secret facts (channel id, name, handle, avatar URL, which one is
active) live in config.json; refresh tokens and the OAuth client secret
live only in the secure :class:`TokenStore`.
"""

from __future__ import annotations

import threading
from typing import Callable

from core.youtube import oauth
from core.youtube.api import Channel, YouTubeClient
from core.youtube.errors import YouTubeAuthError, YouTubeError
from core.youtube.oauth import ClientConfig, Credentials
from core.youtube.storage import CLIENT_KEY, TokenStore, default_store


def _token_key(channel_id: str) -> str:
    return f"channel:{channel_id}"


class YouTubeService:
    def __init__(self, config, store: TokenStore | None = None) -> None:
        self.config = config
        self.store = store if store is not None else default_store()
        self._cache: dict[str, Credentials] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ client
    @property
    def client(self) -> ClientConfig | None:
        data = self.store.get(CLIENT_KEY)
        try:
            return ClientConfig.from_dict(data) if data else None
        except YouTubeError:
            return None

    def set_client(self, client: ClientConfig) -> None:
        self.store.set(CLIENT_KEY, client.to_dict())

    # ------------------------------------------------------------ accounts
    @property
    def accounts(self) -> list[Channel]:
        result = []
        for item in self.config.youtube_accounts:
            try:
                result.append(Channel.from_dict(item))
            except (KeyError, TypeError):
                continue
        return result

    @property
    def active(self) -> Channel | None:
        accounts = self.accounts
        return next((c for c in accounts if c.id == self.config.youtube_active), accounts[0] if accounts else None)

    def set_active(self, channel_id: str) -> None:
        if any(c.id == channel_id for c in self.accounts):
            self.config.youtube_active = channel_id

    def has_token(self, channel_id: str) -> bool:
        return channel_id in self._cache or self.store.get(_token_key(channel_id)) is not None

    def status(self, channel_id: str) -> str:
        if not self.has_token(channel_id):
            return "Sign-in needed"
        if not self.store.persistent:
            return "Connected (this session only)"
        return "Connected"

    # ------------------------------------------------------------ sign-in
    def authorize(
        self, open_browser: Callable[[str], object], cancel: threading.Event | None = None
    ) -> tuple[Credentials, list[Channel]]:
        """Worker-thread part of connecting: browser sign-in + channel lookup."""
        client = self.client
        if client is None:
            raise YouTubeError("Load the OAuth client file (client_secret_….json) first.")
        creds = oauth.sign_in(client, open_browser, cancel)
        channels = YouTubeClient(lambda force: creds.access_token).my_channels()
        if not channels:
            raise YouTubeError("This Google account has no YouTube channel. Create one, then connect again.")
        return creds, channels

    def add_account(self, creds: Credentials, channel: Channel) -> None:
        """GUI-thread part: remember the channel and store its token securely."""
        with self._lock:
            self._cache[channel.id] = creds
        self.store.set(_token_key(channel.id), {"refresh_token": creds.refresh_token})
        accounts = [c for c in self.accounts if c.id != channel.id] + [channel]
        self.config.youtube_accounts = [c.to_dict() for c in accounts]
        self.config.youtube_active = channel.id

    def update_channel(self, channel: Channel) -> None:
        self.config.youtube_accounts = [
            (channel if c.id == channel.id else c).to_dict() for c in self.accounts
        ]

    def disconnect(self, channel_id: str) -> str | None:
        """Forget the account locally; returns the token to revoke at Google (in a worker)."""
        stored = self.store.get(_token_key(channel_id)) or {}
        with self._lock:
            cached = self._cache.pop(channel_id, None)
        token = stored.get("refresh_token") or (cached.refresh_token if cached else None)
        self.store.delete(_token_key(channel_id))
        self.config.youtube_accounts = [c.to_dict() for c in self.accounts if c.id != channel_id]
        if self.config.youtube_active == channel_id:
            self.config.youtube_active = self.accounts[0].id if self.accounts else ""
        return token

    # ------------------------------------------------------------ tokens
    def access_token(self, channel_id: str, force_refresh: bool = False) -> str:
        """A valid access token, refreshed when needed (thread-safe)."""
        with self._lock:
            creds = self._cache.get(channel_id)
            if creds is not None and not force_refresh and not creds.expired:
                return creds.access_token
            client = self.client
            refresh_token = creds.refresh_token if creds else (self.store.get(_token_key(channel_id)) or {}).get(
                "refresh_token")
            if client is None or not refresh_token:
                raise YouTubeAuthError("This channel is not connected. Open Settings → YouTube and connect it.")
            fresh = oauth.refresh(client, refresh_token)
            self._cache[channel_id] = fresh
            return fresh.access_token

    def client_for(self, channel_id: str) -> YouTubeClient:
        return YouTubeClient(lambda force: self.access_token(channel_id, force))
