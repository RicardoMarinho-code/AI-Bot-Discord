"""Migra os slash commands de UM servidor para globais (multi-servidor).

Com `GUILD_ID` preenchido no `.env`, o py-cord registra tudo como
`debug_guilds`: os comandos existem SÓ naquele servidor e o bot entra mudo em
qualquer outro. Esvaziar a variável torna os comandos globais — mas os antigos
de guild continuam registrados no Discord e aparecem DUPLICADOS na lista até
alguém apagá-los. Este script apaga.

Uso (na VM, com o bot parado ou não — a API é independente do gateway):

    .venv/bin/python src/migrar_para_global.py --listar
    .venv/bin/python src/migrar_para_global.py --apagar

Depois: esvazie `GUILD_ID` no `.env` e reinicie. Os comandos globais levam
até 1h para propagar na primeira vez — nesse intervalo o servidor fica sem
comandos. É o preço da mudança, e é uma vez só.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover
    pass

import aiohttp  # noqa: E402

import config  # noqa: E402

API = "https://discord.com/api/v10"


async def _pedir(sessao, metodo: str, caminho: str, corpo=None):
    async with sessao.request(metodo, API + caminho, json=corpo) as resp:
        if resp.status == 204:
            return None
        corpo = await resp.json()
        if resp.status >= 400:
            raise RuntimeError(f"{metodo} {caminho} -> {resp.status}: {corpo}")
        return corpo


async def _run(apagar: bool) -> int:
    if not config.DISCORD_TOKEN:
        print("DISCORD_TOKEN vazio no .env")
        return 2
    if not config.GUILD_ID:
        print("GUILD_ID já está vazio — nada a migrar. Só reinicie o bot.")
        return 0

    headers = {"Authorization": f"Bot {config.DISCORD_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as sessao:
        eu = await _pedir(sessao, "GET", "/users/@me")
        app_id, guild = eu["id"], config.GUILD_ID

        do_guild = await _pedir(sessao, "GET", f"/applications/{app_id}/guilds/{guild}/commands")
        globais = await _pedir(sessao, "GET", f"/applications/{app_id}/commands")

        print(f"bot: {eu['username']} ({app_id})")
        print(f"servidor {guild}: {len(do_guild)} comando(s) — {sorted(c['name'] for c in do_guild)}")
        print(f"globais:          {len(globais)} comando(s) — {sorted(c['name'] for c in globais)}")

        if not apagar:
            print("\n(nada foi alterado; rode com --apagar para remover os do servidor)")
            return 0
        if not do_guild:
            print("\nnada a apagar.")
            return 0

        # bulk overwrite com lista vazia: uma chamada só, sem rate limit por comando
        await _pedir(sessao, "PUT", f"/applications/{app_id}/guilds/{guild}/commands", [])
        print(f"\napagados os {len(do_guild)} comandos do servidor {guild}.")
        print("Agora esvazie GUILD_ID no .env e reinicie:")
        print("  sed -i 's/^GUILD_ID=.*/GUILD_ID=/' ~/bot-discord/.env")
        print("  sudo systemctl restart cabrunco")
        print("A propagação dos globais leva até 1h na primeira vez.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--listar", action="store_true", help="só mostra o que existe hoje")
    grupo.add_argument("--apagar", action="store_true", help="apaga os comandos do servidor")
    args = ap.parse_args()
    return asyncio.run(_run(apagar=args.apagar))


if __name__ == "__main__":
    sys.exit(main())
