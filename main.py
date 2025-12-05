import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, GUILD_OBJECT, TOKEN
from database import (
    compact_backups_once,
    db_add_category,
    db_get_all_categories,
    db_get_backups_for_user,
    get_db_pool,
)
from permissions import (
    MissingWikiPermission,
    has_wiki_admin_role,
    has_wiki_editor_or_admin,
    is_allowed_guild,
)
from views import BackupListView, CategoryDeletePickerView, CategoryPickerView

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)


@tasks.loop(hours=24)
async def backup_maintenance_task():
    try:
        await compact_backups_once()
    except Exception as e:
        print("❌ 백업 정리 작업 중 오류:", e)


@backup_maintenance_task.before_loop
async def before_backup_maintenance_task():
    await bot.wait_until_ready()
    print("⏱️ 백업 정리 작업 대기 완료. 봇 준비 후 24시간 간격으로 실행됩니다.")


@bot.tree.command(
    name="wiki_new",
    description="위키에 새로운 정보를 등록합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_new(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다. `/wiki_category_add` 로 먼저 카테고리를 추가해 주세요.",
            ephemeral=True,
        )
        return

    view = CategoryPickerView(
        mode="new",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        view.get_header_text(),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_view",
    description="위키에 등록된 정보를 조회합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_view(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다.",
            ephemeral=True,
        )
        return

    view = CategoryPickerView(
        mode="view",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        view.get_header_text(),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_edit",
    description="위키에 등록된 정보를 수정합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_edit(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다.",
            ephemeral=True,
        )
        return

    view = CategoryPickerView(
        mode="edit",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        "✏️ 정말로 해당 정보를 수정하시겠습니까?\n" + view.get_header_text(),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_delete",
    description="위키에 등록된 정보를 삭제합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
async def wiki_delete(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다.",
            ephemeral=True,
        )
        return

    view = CategoryPickerView(
        mode="delete",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        "🗑️ 정말로 해당 정보를 삭제하시겠습니까?\n" + view.get_header_text(),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_category_add",
    description="새 카테고리를 추가합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)  # 에디터 OR 관리자
@app_commands.describe(
    name="카테고리 이름",
    description="(선택) 카테고리 설명 / 비고",
)
async def wiki_category_add(
    interaction: discord.Interaction,
    name: str,
    description: Optional[str] = None,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    status = await db_add_category(guild.id, name.strip(), (description or "").strip() or None)
    if status == "dup":
        await interaction.response.send_message(
            f"❗ `{name}` 카테고리가 이미 존재합니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ `{name}` 카테고리를 추가했습니다.",
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_category_delete",
    description="카테고리를 삭제합니다. (카테고리속 등록된 모든 정보도 함께 삭제됩니다!!!)",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
async def wiki_category_delete(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다.",
            ephemeral=True,
        )
        return

    view = CategoryDeletePickerView(
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        "⚠️ 카테고리를 삭제할 시 카테고리내에 등록된 모든 정보가 삭제됩니다!\n"
        "삭제할 카테고리를 선택해 주세요.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_backup_restore",
    description="(개인용) 최근 수정/삭제했던 내용을 되돌립니다. (최대 5개 중 선택)",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_backup_restore(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    backups = await db_get_backups_for_user(guild.id, interaction.user.id, limit=5)
    if not backups:
        await interaction.response.send_message(
            "복구 가능한 백업 데이터가 없습니다.\n"
            "백업은 데이터 정리(24시간 주기) 이후에는 사용할 수 없으며,\n"
            "정리 이후에 새로 수정/삭제한 내역만 복구할 수 있습니다.",
            ephemeral=True,
        )
        return

    lines = []
    for idx, b in enumerate(backups, start=1):
        op_type = b["op_type"]
        if op_type == "edit":
            op_label = "수정"
        elif op_type == "delete":
            op_label = "삭제"
        else:
            op_label = op_type

        ts = b["backed_at"]
        if isinstance(ts, datetime.datetime):
            time_str = ts.strftime("%Y-%m-%d %H:%M")
        else:
            time_str = str(ts)

        lines.append(
            f"{idx}. [{op_label}] [{b['category_name']}] {b['title']} ({time_str})"
        )

    text = (
        "📦 최근 수정/삭제 내역 (최대 5개)\n"
        + "\n".join(lines)
        + "\n\n복원할 항목을 선택해 주세요."
    )

    view = BackupListView(
        guild_id=guild.id,
        requester_id=interaction.user.id,
        backups=backups,
    )

    await interaction.response.send_message(
        text,
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_snapshot_restore",
    description="(관리자용) 데이터 정리 시점 스냅샷(최대 3일)을 사용해 글을 복원합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
async def wiki_snapshot_restore(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    categories = await db_get_all_categories(guild.id)
    if not categories:
        await interaction.response.send_message(
            "아직 등록된 카테고리가 없습니다.",
            ephemeral=True,
        )
        return

    view = CategoryPickerView(
        mode="snapshot_restore",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )
    await interaction.response.send_message(
        view.get_header_text(),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_cleanup_status",
    description="다음 데이터 정리까지 남은 시간을 확인합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_cleanup_status(interaction: discord.Interaction):
    if not backup_maintenance_task.is_running():
        await interaction.response.send_message(
            "데이터 정리 작업이 아직 시작되지 않았습니다.",
            ephemeral=True,
        )
        return

    next_iter = backup_maintenance_task.next_iteration
    if next_iter is None:
        await interaction.response.send_message(
            "다음 데이터 정리 시각을 계산 중입니다.",
            ephemeral=True,
        )
        return

    now = discord.utils.utcnow()
    delta = next_iter - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        msg = "곧 데이터 정리 작업이 실행될 예정입니다."
    else:
        days = total_seconds // 86400
        remain = total_seconds % 86400
        hours = remain // 3600
        remain %= 3600
        minutes = remain // 60
        seconds = remain % 60

        parts = []
        if days:
            parts.append(f"{days}일")
        if hours:
            parts.append(f"{hours}시간")
        if minutes:
            parts.append(f"{minutes}분")
        if seconds or not parts:
            parts.append(f"{seconds}초")

        human = " ".join(parts)
        msg = f"⏱️ 다음 데이터 정리까지 남은 시간: **{human}**"

    await interaction.response.send_message(
        msg,
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    # 필요한 디스코드 역할이 없을 때
    if isinstance(error, MissingWikiPermission):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "해당 명령어를 사용가능한 권한이 없습니다.",
                    ephemeral=True,
                )
        except Exception:
            pass
        return

    # 그 외 체크 실패 (예: 다른 서버, DM 등)
    if isinstance(error, app_commands.CheckFailure):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "이 봇은 지정된 서버에서만 사용할 수 있습니다.",
                    ephemeral=True,
                )
        except Exception:
            pass
        return

    # 디버깅용 로그
    print("App command error:", repr(error))


@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")
    try:
        await get_db_pool()
        print("✅ DB 초기화 완료")

        synced = await bot.tree.sync(guild=GUILD_OBJECT)
        print(f"✅ 슬래시 명령어 {len(synced)}개 길드 동기화 완료 (guild_id={ALLOWED_GUILD_ID})")
        print("✅ 봇 준비 완료 & 슬래시 명령어 동기화 완료")

        if not backup_maintenance_task.is_running():
            backup_maintenance_task.start()
            print("⏱️ 백업 정리 작업 시작 (24시간 간격)")
    except Exception as e:
        print("❌ 초기화 중 오류:", e)


if __name__ == "__main__":
    bot.run(TOKEN)
