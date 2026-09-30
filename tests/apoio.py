"""Dublês compartilhados pelos testes: voz, servidor, canal e o assistente."""
from __future__ import annotations

from unittest.mock import MagicMock

import discord

from core.assistente import Assistente


class FakeVoice:
    """Voz conectada e parada — o ponto de partida da maioria dos testes.

    Anota o que o bot tocou (`tocados`), quantas vezes cortou o som
    (`paradas`) e se desconectou.
    """

    def __init__(self, *, playing: bool = False, connected: bool = True, channel=None,
                 recording: bool = False) -> None:
        self.playing = playing
        self.connected = connected
        self.channel = channel
        self.recording = recording
        self.tocados: list = []
        self.paradas = 0
        self.desconectou = False
        self.after = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return False

    def is_recording(self) -> bool:
        return self.recording

    def play(self, fonte, after=None) -> None:
        self.tocados.append(fonte)
        self.playing = True
        self.after = after

    def stop_playing(self) -> None:
        self.paradas += 1
        self.playing = False

    def stop_recording(self) -> None:
        self.recording = False

    async def disconnect(self, force: bool = False) -> None:
        self.desconectou = True
        self.connected = False


class FakeMember:
    def __init__(self, user_id: int, nome: str, *, bot: bool = False) -> None:
        self.id = user_id
        self.display_name = nome
        self.bot = bot


class FakeGuild:
    """Servidor com membros no cache (`membros`) e outros só pela API (`remotos`)."""

    def __init__(self, voice=None, guild_id: int = 1, *, membros=(), remotos=()) -> None:
        self.id = guild_id
        self.voice_client = voice
        self._cache = {m.id: m for m in membros}
        self._api = {m.id: m for m in remotos}
        self.buscados: list[int] = []

    def get_member(self, user_id: int):
        return self._cache.get(user_id)

    async def fetch_member(self, user_id: int):
        self.buscados.append(user_id)
        if user_id not in self._api:
            raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Member")
        return self._api[user_id]


class FakeChannel:
    """Canal de texto que guarda o que o bot mandou."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


_SEM_VOZ = object()


def faz_assistente(*, guild=None, voice=_SEM_VOZ, channel=None, bot=None) -> Assistente:
    """Um Assistente de verdade, sem Discord. `guild` OU `voice` (com `voice`,
    monta um FakeGuild em volta); `channel` é o canal de texto."""
    if guild is None:
        guild = FakeGuild(FakeVoice() if voice is _SEM_VOZ else voice)
    sessao = Assistente(bot or MagicMock(), guild)
    sessao.text_channel = channel
    return sessao
