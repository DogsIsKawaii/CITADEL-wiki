import os

import discord


def env_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} 환경 변수가 설정되지 않았습니다.")
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} 환경 변수 값이 정수가 아닙니다: {value}")


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")

ALLOWED_GUILD_ID = env_int("ALLOWED_GUILD_ID")
WIKI_ADMIN_ROLE_ID = env_int("WIKI_ADMIN_ROLE_ID")
WIKI_EDITOR_ROLE_ID = env_int("WIKI_EDITOR_ROLE_ID")

GUILD_OBJECT = discord.Object(id=ALLOWED_GUILD_ID)
