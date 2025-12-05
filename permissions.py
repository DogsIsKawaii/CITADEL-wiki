import discord
from discord import app_commands

from config import ALLOWED_GUILD_ID, WIKI_ADMIN_ROLE_ID, WIKI_EDITOR_ROLE_ID


class MissingWikiPermission(app_commands.CheckFailure):
    """필요한 위키 역할이 없을 때 발생시키는 예외"""


def is_allowed_guild(interaction: discord.Interaction) -> bool:
    """허용된 길드(서버)인지 체크"""
    return interaction.guild is not None and interaction.guild.id == ALLOWED_GUILD_ID


def has_wiki_admin_role(interaction: discord.Interaction) -> bool:
    """삭제/카테고리 삭제/스냅샷 복구 등 관리자 전용 역할 체크"""
    if not isinstance(interaction.user, discord.Member):
        raise MissingWikiPermission()
    if not any(role.id == WIKI_ADMIN_ROLE_ID for role in interaction.user.roles):
        raise MissingWikiPermission()
    return True


def has_wiki_editor_role(interaction: discord.Interaction) -> bool:
    """에디터 전용 역할 체크 (현재는 개별 데코레이터에서는 사용 X, 참고용)"""
    if not isinstance(interaction.user, discord.Member):
        raise MissingWikiPermission()
    if not any(role.id == WIKI_EDITOR_ROLE_ID for role in interaction.user.roles):
        raise MissingWikiPermission()
    return True


def has_wiki_editor_or_admin(interaction: discord.Interaction) -> bool:
    """에디터 또는 관리자 중 하나라도 있으면 통과"""
    if not isinstance(interaction.user, discord.Member):
        raise MissingWikiPermission()
    role_ids = {role.id for role in interaction.user.roles}
    if (WIKI_EDITOR_ROLE_ID not in role_ids) and (WIKI_ADMIN_ROLE_ID not in role_ids):
        raise MissingWikiPermission()
    return True
