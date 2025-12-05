import datetime
import math
from typing import List

import asyncpg
import discord

from database import (
    db_check_backup_conflict,
    db_delete_article,
    db_delete_category,
    db_edit_article,
    db_get_article_for_view,
    db_get_articles_in_category,
    db_get_snapshots_for_article,
    db_search_articles,
    db_upsert_article,
    get_db_pool,
)
from utils import build_article_embeds, send_embeds_with_chunking


class NewArticleModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"[{category}] 새 위키 글 작성")
        self.category = category

        self.title_input = discord.ui.TextInput(
            label="제목",
            max_length=100,
        )
        self.content_input = discord.ui.TextInput(
            label="내용",
            style=discord.TextStyle.paragraph,
            max_length=2000,
        )

        self.add_item(self.title_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message(
                "길드 안에서만 사용할 수 있어요.",
                ephemeral=True,
            )
            return

        title = self.title_input.value.strip()
        content = self.content_input.value.strip()

        if not title or not content:
            await interaction.response.send_message(
                "제목과 내용을 모두 입력해 주세요.",
                ephemeral=True,
            )
            return

        try:
            status, contrib_count = await db_upsert_article(
                guild.id,
                self.category,
                title,
                content,
                user.id,
                getattr(user, "display_name", str(user)),
            )
        except ValueError:
            await interaction.response.send_message(
                "선택한 카테고리가 존재하지 않습니다. `/wiki_category_add` 로 먼저 카테고리를 만들어 주세요.",
                ephemeral=True,
            )
            return

        if status == "dup":
            await interaction.response.send_message(
                "❗ 동일한 제목의 글이 이미 존재합니다.\n"
                "제목을 변경하시거나 `/wiki_edit` 명령어로 기존 글을 수정해 주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ [{self.category}] `{title}` 저장 완료! (새 글이 등록되었습니다)\n"
            f"작성자: {user.mention} (이 글에 {contrib_count}번째 기여)",
            ephemeral=True,
        )


class EditConfirmView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        category_name: str,
        old_title: str,
        new_title: str,
        new_content: str,
        requester_id: int,
    ):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.category_name = category_name
        self.old_title = old_title
        self.new_title = new_title
        self.new_content = new_content
        self.requester_id = requester_id

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 확인 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="예", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return

        status, contrib = await db_edit_article(
            self.guild_id,
            self.category_name,
            self.old_title,
            self.new_title,
            self.new_content,
            interaction.user.id,
        )

        if status == "no_category":
            await interaction.response.edit_message(
                content="카테고리를 찾을 수 없어 수정에 실패했습니다.",
                view=None,
            )
            return
        if status == "no_article":
            await interaction.response.edit_message(
                content="대상 글을 찾을 수 없어 수정에 실패했습니다.",
                view=None,
            )
            return
        if status == "dup_title":
            await interaction.response.edit_message(
                content="❗ 동일한 제목의 글이 이미 존재합니다. 제목을 변경해 주세요.",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=(
                f"✅ `{self.old_title}` → `{self.new_title}` 글이 수정되었습니다.\n"
                f"{interaction.user.mention} 이(가) 이 글에 {contrib}번째 기여를 했습니다."
            ),
            view=None,
        )

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(
            content="수정을 취소했습니다.",
            view=None,
        )


class EditArticleModal(discord.ui.Modal):
    def __init__(self, guild_id: int, category_name: str, title: str, content: str):
        super().__init__(title=f"[{category_name}] 글 수정: {title}")
        self.guild_id = guild_id
        self.category_name = category_name
        self.old_title = title

        self.title_input = discord.ui.TextInput(
            label="제목",
            max_length=100,
            default=title,
        )
        self.content_input = discord.ui.TextInput(
            label="내용",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            default=content,
        )

        self.add_item(self.title_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_title = self.title_input.value.strip()
        new_content = self.content_input.value.strip()

        if not new_title or not new_content:
            await interaction.response.send_message(
                "제목과 내용은 비어 있을 수 없습니다.",
                ephemeral=True,
            )
            return

        view = EditConfirmView(
            guild_id=self.guild_id,
            category_name=self.category_name,
            old_title=self.old_title,
            new_title=new_title,
            new_content=new_content,
            requester_id=interaction.user.id,
        )

        await interaction.response.send_message(
            "✏️ 정말로 해당 정보를 수정하시겠습니까?",
            view=view,
            ephemeral=True,
        )


class DeleteConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, category: str, title: str, requester_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.category = category
        self.title = title
        self.requester_id = requester_id

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 확인 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="예", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return

        status = await db_delete_article(
            self.guild_id,
            self.category,
            self.title,
            interaction.user.id,
        )

        if status == "no_category":
            await interaction.response.edit_message(
                content="카테고리를 찾을 수 없어 삭제에 실패했습니다.",
                view=None,
            )
            return
        if status == "no_article":
            await interaction.response.edit_message(
                content="대상 글을 찾을 수 없어 삭제에 실패했습니다.",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=f"🗑️ 정말로 해당 정보를 삭제하시겠습니까?\n\n✅ [{self.category}] `{self.title}` 글이 삭제되었습니다.",
            view=None,
        )

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(
            content="삭제를 취소했습니다.",
            view=None,
        )


class RestoreBackupView(discord.ui.View):
    def __init__(
        self,
        backup_id: int,
        guild_id: int,
        category_name: str,
        title: str,
        requester_id: int,
    ):
        super().__init__(timeout=60)
        self.backup_id = backup_id
        self.guild_id = guild_id
        self.category_name = category_name
        self.title = title
        self.requester_id = requester_id

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 복구 창은 백업을 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    async def _restore(self, interaction: discord.Interaction):
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                backup = await conn.fetchrow(
                    """
                    SELECT id, article_id, category_name, title, content,
                           created_by_id, created_by_name, created_at, updated_at,
                           op_type, actor_id
                    FROM wiki_article_backups
                    WHERE id=$1
                    """,
                    self.backup_id,
                )
                if not backup:
                    await interaction.response.edit_message(
                        content="해당 백업 데이터를 찾을 수 없습니다.",
                        view=None,
                    )
                    return

                cat_row = await conn.fetchrow(
                    "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                    self.guild_id,
                    backup["category_name"],
                )
                if not cat_row:
                    cat_row = await conn.fetchrow(
                        """
                        INSERT INTO wiki_categories (guild_id, name)
                        VALUES ($1, $2)
                        RETURNING id
                        """,
                        self.guild_id,
                        backup["category_name"],
                    )
                category_id = cat_row["id"]

                article_id = backup["article_id"]
                if article_id is not None:
                    current = await conn.fetchrow(
                        "SELECT id FROM wiki_articles WHERE id=$1",
                        article_id,
                    )
                else:
                    current = None

                if current:
                    await conn.execute(
                        """
                        UPDATE wiki_articles
                        SET category_id=$1,
                            title=$2,
                            content=$3,
                            created_by_id=$4,
                            created_by_name=$5,
                            created_at=$6,
                            updated_at=$7
                        WHERE id=$8
                        """,
                        category_id,
                        backup["title"],
                        backup["content"],
                        backup["created_by_id"],
                        backup["created_by_name"],
                        backup["created_at"] or discord.utils.utcnow(),
                        backup["updated_at"] or discord.utils.utcnow(),
                        article_id,
                    )
                else:
                    art_row = await conn.fetchrow(
                        """
                        INSERT INTO wiki_articles
                            (guild_id, category_id, title, content,
                             created_by_id, created_by_name, created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        RETURNING id
                        """,
                        self.guild_id,
                        category_id,
                        backup["title"],
                        backup["content"],
                        backup["created_by_id"],
                        backup["created_by_name"],
                        backup["created_at"] or discord.utils.utcnow(),
                        backup["updated_at"] or discord.utils.utcnow(),
                    )
                    article_id = art_row["id"]

                # 사용한 개인 백업은 삭제
                await conn.execute(
                    "DELETE FROM wiki_article_backups WHERE id=$1",
                    backup["id"],
                )

        await interaction.response.edit_message(
            content=f"✅ [{self.category_name}] `{self.title}` 글을 직전 상태로 복원했습니다.",
            view=None,
        )

    @discord.ui.button(label="예", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await self._restore(interaction)

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(
            content="복원을 취소했습니다.",
            view=None,
        )


class BackupListView(discord.ui.View):
    """
    최근 개인 백업 5개 목록을 Select로 보여주고,
    선택한 백업에 대해 RestoreBackupView로 복구 여부를 물어보는 뷰.
    """

    def __init__(
        self,
        guild_id: int,
        requester_id: int,
        backups: List[asyncpg.Record],
    ):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.backups = backups

        options: List[discord.SelectOption] = []
        for b in backups:
            op_type = b["op_type"]
            if op_type == "edit":
                op_label = "수정"
            elif op_type == "delete":
                op_label = "삭제"
            else:
                op_label = op_type

            label = f"[{op_label}] [{b['category_name']}] {b['title']}"
            if len(label) > 100:
                label = label[:97] + "..."

            ts = b["backed_at"]
            if isinstance(ts, datetime.datetime):
                time_str = ts.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = str(ts)

            options.append(
                discord.SelectOption(
                    label=label,
                    description=time_str,
                    value=str(b["id"]),
                )
            )

        self.select = discord.ui.Select(
            placeholder="복원할 백업을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        cancel_btn = discord.ui.Button(
            label="취소",
            style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return

        backup_id = int(self.select.values[0])

        target = None
        for b in self.backups:
            if b["id"] == backup_id:
                target = b
                break

        if target is None:
            await interaction.response.send_message(
                "선택한 백업 데이터를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        op_type = target["op_type"]
        if op_type == "edit":
            op_label = "수정"
        elif op_type == "delete":
            op_label = "삭제"
        else:
            op_label = op_type

        category_name = target["category_name"]
        title = target["title"]

        # 🔍 다른 사용자가 이후에 수정/삭제했는지 확인
        conflict_type, other_user_id = await db_check_backup_conflict(backup_id)

        if other_user_id:
            other_mention = f"<@{other_user_id}>"
        else:
            other_mention = "알 수 없는 사용자"

        if conflict_type == "edited_by_other":
            conflict_text = (
                f"⚠️ 다른 사용자가 해당 정보를 수정하였습니다. (마지막 수정자: {other_mention})\n"
                "정말로 백업하시겠습니까?"
            )
        elif conflict_type == "deleted_by_other":
            conflict_text = (
                f"⚠️ 다른 사용자가 해당 정보를 삭제하였습니다. (삭제한 사용자: {other_mention})\n"
                "정말로 백업하시겠습니까?"
            )
        else:
            conflict_text = "해당 정보를 이 상태로 되돌리겠습니까?"

        text = (
            "📦 선택한 백업 내역\n"
            f"- 작업 종류: **{op_label}**\n"
            f"- 카테고리: `{category_name}`\n"
            f"- 제목: `{title}`\n\n"
            f"{conflict_text}"
        )

        view = RestoreBackupView(
            backup_id=backup_id,
            guild_id=self.guild_id,
            category_name=category_name,
            title=title,
            requester_id=self.requester_id,
        )

        await interaction.response.send_message(
            text,
            view=view,
            ephemeral=True,
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return

        await interaction.response.edit_message(
            content="백업 복원을 취소했습니다.",
            view=None,
        )


class SnapshotRestoreView(discord.ui.View):
    """
    스냅샷(3일 보관용)에서 글을 실제 데이터로 되돌리는 확인 뷰
    """

    def __init__(
        self,
        snapshot_id: int,
        guild_id: int,
        category_name: str,
        title: str,
        requester_id: int,
    ):
        super().__init__(timeout=60)
        self.snapshot_id = snapshot_id
        self.guild_id = guild_id
        self.category_name = category_name
        self.title = title
        self.requester_id = requester_id

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 복구 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    async def _restore(self, interaction: discord.Interaction):
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                snap = await conn.fetchrow(
                    """
                    SELECT id, guild_id, article_id, category_name, title, content,
                           created_by_id, created_by_name, created_at, updated_at, snapshot_at
                    FROM wiki_snapshot_backups
                    WHERE id=$1 AND guild_id=$2
                    """,
                    self.snapshot_id,
                    self.guild_id,
                )
                if not snap:
                    await interaction.response.edit_message(
                        content="해당 스냅샷 데이터를 찾을 수 없습니다.",
                        view=None,
                    )
                    return

                # 카테고리 존재 확인/생성
                cat_row = await conn.fetchrow(
                    "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                    self.guild_id,
                    snap["category_name"],
                )
                if not cat_row:
                    cat_row = await conn.fetchrow(
                        """
                        INSERT INTO wiki_categories (guild_id, name)
                        VALUES ($1, $2)
                        RETURNING id
                        """,
                        self.guild_id,
                        snap["category_name"],
                    )
                category_id = cat_row["id"]

                article_id = snap["article_id"]
                if article_id is not None:
                    current = await conn.fetchrow(
                        "SELECT id FROM wiki_articles WHERE id=$1",
                        article_id,
                    )
                else:
                    current = None

                if current:
                    # 기존 글 덮어쓰기
                    await conn.execute(
                        """
                        UPDATE wiki_articles
                        SET category_id=$1,
                            title=$2,
                            content=$3,
                            created_by_id=$4,
                            created_by_name=$5,
                            created_at=$6,
                            updated_at=$7
                        WHERE id=$8
                        """,
                        category_id,
                        snap["title"],
                        snap["content"],
                        snap["created_by_id"],
                        snap["created_by_name"],
                        snap["created_at"] or discord.utils.utcnow(),
                        snap["updated_at"] or discord.utils.utcnow(),
                        article_id,
                    )
                else:
                    # 글이 없어졌다면 새로 생성
                    art_row = await conn.fetchrow(
                        """
                        INSERT INTO wiki_articles
                            (guild_id, category_id, title, content,
                             created_by_id, created_by_name, created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        RETURNING id
                        """,
                        self.guild_id,
                        category_id,
                        snap["title"],
                        snap["content"],
                        snap["created_by_id"],
                        snap["created_by_name"],
                        snap["created_at"] or discord.utils.utcnow(),
                        snap["updated_at"] or discord.utils.utcnow(),
                    )
                    article_id = art_row["id"]

        await interaction.response.edit_message(
            content=f"✅ [{self.category_name}] `{self.title}` 글을 선택한 스냅샷 상태로 복원했습니다.",
            view=None,
        )

    @discord.ui.button(label="예", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await self._restore(interaction)

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(
            content="스냅샷 복원을 취소했습니다.",
            view=None,
        )


class SnapshotListView(discord.ui.View):
    """
    특정 글에 대해 최근 3일 안에 저장된 스냅샷 목록을 보여서
    하나를 선택하고 '정말로 정보를 백업하겠습니까?' 를 묻는 뷰
    (관리자 전용)
    """

    def __init__(
        self,
        guild_id: int,
        category_name: str,
        title: str,
        requester_id: int,
        snapshots: List[asyncpg.Record],
    ):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.category_name = category_name
        self.title = title
        self.requester_id = requester_id
        self.snapshots = snapshots

        options = []
        for s in snapshots:
            ts = s["snapshot_at"]
            if isinstance(ts, datetime.datetime):
                time_str = ts.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = str(ts)

            label = f"{self.title}"
            if len(label) > 90:
                label = label[:87] + "..."

            options.append(
                discord.SelectOption(
                    label=label,
                    description=f"스냅샷 시각: {time_str}",
                    value=str(s["id"]),
                )
            )

        self.select = discord.ui.Select(
            placeholder="복원할 스냅샷을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        cancel_btn = discord.ui.Button(
            label="취소",
            style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return

        snapshot_id = int(self.select.values[0])

        target = None
        for s in self.snapshots:
            if s["id"] == snapshot_id:
                target = s
                break

        if target is None:
            await interaction.response.send_message(
                "선택한 스냅샷 데이터를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        ts = target["snapshot_at"]
        if isinstance(ts, datetime.datetime):
            time_str = ts.strftime("%Y-%m-%d %H:%M")
        else:
            time_str = str(ts)

        text = (
            "📦 선택한 스냅샷 정보\n"
            f"- 카테고리: `{self.category_name}`\n"
            f"- 제목: `{self.title}`\n"
            f"- 스냅샷 시각: `{time_str}`\n\n"
            "정말로 정보를 백업하겠습니까?"
        )

        view = SnapshotRestoreView(
            snapshot_id=snapshot_id,
            guild_id=self.guild_id,
            category_name=self.category_name,
            title=self.title,
            requester_id=self.requester_id,
        )

        await interaction.response.send_message(
            text,
            view=view,
            ephemeral=True,
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return

        await interaction.response.edit_message(
            content="스냅샷 복원을 취소했습니다.",
            view=None,
        )


class SearchModal(discord.ui.Modal):
    def __init__(self, mode: str, guild_id: int, requester_id: int):
        title_map = {
            "view": "위키 검색 (조회)",
            "edit": "위키 검색 (수정)",
            "delete": "위키 검색 (삭제)",
            "snapshot_restore": "위키 검색 (스냅샷 복원)",
        }
        super().__init__(title=title_map.get(mode, "위키 검색"))

        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id

        self.query_input = discord.ui.TextInput(
            label="검색어",
            placeholder="카테고리 / 제목 / 내용 일부를 입력하세요",
            max_length=100,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "길드 안에서만 사용할 수 있어요.",
                ephemeral=True,
            )
            return

        query = self.query_input.value.strip()
        if not query:
            await interaction.response.send_message(
                "검색어를 입력해 주세요.",
                ephemeral=True,
            )
            return

        rows = await db_search_articles(guild.id, query, limit=10)
        if not rows:
            await interaction.response.send_message(
                "검색 결과가 없습니다.",
                ephemeral=True,
            )
            return

        view = SearchResultView(
            mode=self.mode,
            guild_id=guild.id,
            requester_id=self.requester_id,
            results=rows,
        )

        if self.mode == "view":
            action_text = "조회할 글을 선택해 주세요."
        elif self.mode == "edit":
            action_text = "수정할 글을 선택해 주세요."
        elif self.mode == "delete":
            action_text = "삭제할 글을 선택해 주세요."
        elif self.mode == "snapshot_restore":
            action_text = "스냅샷에서 복원할 글을 선택해 주세요."
        else:
            action_text = "처리할 글을 선택해 주세요."

        lines = [f"- [{r['category_name']}] {r['title']}" for r in rows]

        text = (
            "🔍 검색 결과 (최대 10개):\n"
            + "\n".join(lines)
            + "\n\n"
            + action_text
        )

        await interaction.response.send_message(
            text,
            view=view,
            ephemeral=True,
        )


class SearchResultView(discord.ui.View):
    def __init__(
        self,
        mode: str,
        guild_id: int,
        requester_id: int,
        results: List[asyncpg.Record],
    ):
        super().__init__(timeout=60)
        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.results = results

        options = []
        for idx, row in enumerate(results):
            label = f"[{row['category_name']}] {row['title']}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(idx),
                )
            )

        self.select = discord.ui.Select(
            placeholder="글을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return

        idx = int(self.select.values[0])
        row = self.results[idx]
        category_name = row["category_name"]
        title = row["title"]

        # 조회
        if self.mode == "view":
            art_row, contrib_rows = await db_get_article_for_view(
                self.guild_id, category_name, title
            )
            if not art_row:
                await interaction.response.send_message(
                    "해당 글을 찾을 수 없습니다.",
                    ephemeral=True,
                )
                return

            embeds = build_article_embeds(art_row, contrib_rows)
            await send_embeds_with_chunking(interaction, embeds, ephemeral=False)
            return

        # 수정
        if self.mode == "edit":
            art_row, _ = await db_get_article_for_view(
                self.guild_id, category_name, title
            )
            if not art_row:
                await interaction.response.send_message(
                    "해당 글을 찾을 수 없습니다.",
                    ephemeral=True,
                )
                return

            modal = EditArticleModal(
                guild_id=self.guild_id,
                category_name=category_name,
                title=title,
                content=art_row["content"],
            )
            await interaction.response.send_modal(modal)
            return

        # 삭제
        if self.mode == "delete":
            view = DeleteConfirmView(
                guild_id=self.guild_id,
                category=category_name,
                title=title,
                requester_id=self.requester_id,
            )
            await interaction.response.send_message(
                f"🗑️ 정말로 해당 정보를 삭제하시겠습니까?\n\n[{category_name}] `{title}`",
                view=view,
                ephemeral=True,
            )
            return

        # 스냅샷 복원 (관리자용)
        if self.mode == "snapshot_restore":
            snapshots = await db_get_snapshots_for_article(
                self.guild_id, category_name, title, limit=10
            )
            if not snapshots:
                await interaction.response.send_message(
                    "해당 글에 대해 최근 3일 이내에 저장된 스냅샷이 없습니다.",
                    ephemeral=True,
                )
                return

            lines = []
            for i, s in enumerate(snapshots, start=1):
                ts = s["snapshot_at"]
                if isinstance(ts, datetime.datetime):
                    time_str = ts.strftime("%Y-%m-%d %H:%M")
                else:
                    time_str = str(ts)
                lines.append(f"{i}. {title} ({time_str})")

            text = (
                f"📦 `{category_name}` / `{title}` 의 최근 스냅샷 목록입니다.\n"
                "복원할 스냅샷을 선택해 주세요.\n\n"
                + "\n".join(lines)
            )

            view = SnapshotListView(
                guild_id=self.guild_id,
                category_name=category_name,
                title=title,
                requester_id=self.requester_id,
                snapshots=snapshots,
            )
            await interaction.response.send_message(
                text,
                view=view,
                ephemeral=True,
            )
            return


class ArticlePickerView(discord.ui.View):
    def __init__(
        self,
        mode: str,
        guild_id: int,
        requester_id: int,
        category_name: str,
        articles: List[asyncpg.Record],
        page: int = 0,
    ):
        super().__init__(timeout=120)
        self.mode = mode  # "view" / "edit" / "delete" / "snapshot_restore"
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.category_name = category_name
        self.articles = articles
        self.page = page
        self.page_size = 10

        self._build_items()

    def _build_items(self):
        self.clear_items()
        start = self.page * self.page_size
        end = start + self.page_size
        page_articles = self.articles[start:end]

        options = [
            discord.SelectOption(
                label=a["title"][:100],
                value=a["title"],
            )
            for a in page_articles
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label="(글이 없습니다)",
                    value="_none",
                    description="해당 카테고리에 글이 없습니다.",
                )
            ]

        select = discord.ui.Select(
            placeholder="글을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def select_callback(interaction: discord.Interaction):
            await self._on_select(interaction, select)

        select.callback = select_callback
        self.add_item(select)

        prev_btn = discord.ui.Button(
            label="이전",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
        )
        next_btn = discord.ui.Button(
            label="다음",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page + 1) * self.page_size >= len(self.articles),
        )
        search_btn = discord.ui.Button(
            label="검색",
            style=discord.ButtonStyle.primary,
        )

        async def prev_cb(interaction: discord.Interaction):
            await self._change_page(interaction, self.page - 1)

        async def next_cb(interaction: discord.Interaction):
            await self._change_page(interaction, self.page + 1)

        async def search_cb(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    "이 검색 버튼은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True,
                )
                return
            modal = SearchModal(self.mode, self.guild_id, self.requester_id)
            await interaction.response.send_modal(modal)

        prev_btn.callback = prev_cb
        next_btn.callback = next_cb
        search_btn.callback = search_cb

        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(search_btn)

    async def _change_page(self, interaction: discord.Interaction, new_page: int):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 페이지 버튼은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if new_page < 0:
            new_page = 0
        max_page = max(0, math.ceil(len(self.articles) / self.page_size) - 1)
        if new_page > max_page:
            new_page = max_page

        new_view = ArticlePickerView(
            mode=self.mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            category_name=self.category_name,
            articles=self.articles,
            page=new_page,
        )
        await interaction.response.edit_message(
            content=new_view.get_header_text(),
            view=new_view,
        )

    def get_header_text(self) -> str:
        total_pages = max(1, math.ceil(len(self.articles) / self.page_size))
        page_info = f"(페이지 {self.page + 1} / {total_pages})"

        if self.mode == "view":
            action = "조회할 글을 선택해 주세요."
        elif self.mode == "edit":
            action = "수정할 글을 선택해 주세요."
        elif self.mode == "delete":
            action = "삭제할 글을 선택해 주세요."
        elif self.mode == "snapshot_restore":
            action = "스냅샷에서 복원할 글을 선택해 주세요."
        else:
            action = "처리할 글을 선택해 주세요."

        return f"📄 카테고리: `{self.category_name}`\n{action}\n{page_info}"

    async def _on_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        value = select.values[0]
        if value == "_none":
            await interaction.response.send_message(
                "해당 카테고리에 등록된 글이 없습니다.",
                ephemeral=True,
            )
            return

        title = value

        # 조회
        if self.mode == "view":
            art_row, contrib_rows = await db_get_article_for_view(
                self.guild_id, self.category_name, title
            )
            if not art_row:
                await interaction.response.send_message(
                    "해당 글을 찾을 수 없습니다.",
                    ephemeral=True,
                )
                return

            embeds = build_article_embeds(art_row, contrib_rows)
            await send_embeds_with_chunking(interaction, embeds, ephemeral=False)
            return

        # 수정
        if self.mode == "edit":
            art_row, _ = await db_get_article_for_view(
                self.guild_id, self.category_name, title
            )
            if not art_row:
                await interaction.response.send_message(
                    "해당 글을 찾을 수 없습니다.",
                    ephemeral=True,
                )
                return

            modal = EditArticleModal(
                guild_id=self.guild_id,
                category_name=self.category_name,
                title=title,
                content=art_row["content"],
            )
            await interaction.response.send_modal(modal)
            return

        # 삭제
        if self.mode == "delete":
            view = DeleteConfirmView(
                guild_id=self.guild_id,
                category=self.category_name,
                title=title,
                requester_id=self.requester_id,
            )
            await interaction.response.send_message(
                f"🗑️ 정말로 해당 정보를 삭제하시겠습니까?\n\n[{self.category_name}] `{title}`",
                view=view,
                ephemeral=True,
            )
            return

        # 스냅샷 복원
        if self.mode == "snapshot_restore":
            snapshots = await db_get_snapshots_for_article(
                self.guild_id, self.category_name, title, limit=10
            )
            if not snapshots:
                await interaction.response.send_message(
                    "해당 글에 대해 최근 3일 이내에 저장된 스냅샷이 없습니다.",
                    ephemeral=True,
                )
                return

            lines = []
            for i, s in enumerate(snapshots, start=1):
                ts = s["snapshot_at"]
                if isinstance(ts, datetime.datetime):
                    time_str = ts.strftime("%Y-%m-%d %H:%M")
                else:
                    time_str = str(ts)
                lines.append(f"{i}. {title} ({time_str})")

            text = (
                f"📦 `{self.category_name}` / `{title}` 의 최근 스냅샷 목록입니다.\n"
                "복원할 스냅샷을 선택해 주세요.\n\n"
                + "\n".join(lines)
            )

            view = SnapshotListView(
                guild_id=self.guild_id,
                category_name=self.category_name,
                title=title,
                requester_id=self.requester_id,
                snapshots=snapshots,
            )
            await interaction.response.send_message(
                text,
                view=view,
                ephemeral=True,
            )
            return


class CategoryPickerView(discord.ui.View):
    def __init__(
        self,
        mode: str,
        guild_id: int,
        requester_id: int,
        categories: List[asyncpg.Record],
        page: int = 0,
    ):
        super().__init__(timeout=120)
        self.mode = mode  # "new" / "view" / "edit" / "delete" / "snapshot_restore"
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.categories = categories
        self.page = page
        self.page_size = 10

        self._build_items()

    def _build_items(self):
        self.clear_items()
        start = self.page * self.page_size
        end = start + self.page_size
        page_cats = self.categories[start:end]

        options = [
            discord.SelectOption(
                label=c["name"],
                description=(c["description"] or "")[:90] if c["description"] else None,
                value=c["name"],
            )
            for c in page_cats
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label="(카테고리가 없습니다)",
                    value="_none",
                )
            ]

        select = discord.ui.Select(
            placeholder="카테고리를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def select_callback(interaction: discord.Interaction):
            await self._on_select(interaction, select)

        select.callback = select_callback
        self.add_item(select)

        prev_btn = discord.ui.Button(
            label="이전",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
        )
        next_btn = discord.ui.Button(
            label="다음",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page + 1) * self.page_size >= len(self.categories),
        )
        search_btn = discord.ui.Button(
            label="검색",
            style=discord.ButtonStyle.primary,
            disabled=(self.mode == "new"),
        )

        async def prev_cb(interaction: discord.Interaction):
            await self._change_page(interaction, self.page - 1)

        async def next_cb(interaction: discord.Interaction):
            await self._change_page(interaction, self.page + 1)

        async def search_cb(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    "이 검색 버튼은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True,
                )
                return
            modal = SearchModal(self.mode, self.guild_id, self.requester_id)
            await interaction.response.send_modal(modal)

        prev_btn.callback = prev_cb
        next_btn.callback = next_cb
        search_btn.callback = search_cb

        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(search_btn)

    async def _change_page(self, interaction: discord.Interaction, new_page: int):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 페이지 버튼은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if new_page < 0:
            new_page = 0
        max_page = max(0, math.ceil(len(self.categories) / self.page_size) - 1)
        if new_page > max_page:
            new_page = max_page

        new_view = CategoryPickerView(
            mode=self.mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            categories=self.categories,
            page=new_page,
        )
        await interaction.response.edit_message(
            content=new_view.get_header_text(),
            view=new_view,
        )

    def get_header_text(self) -> str:
        total_pages = max(1, math.ceil(len(self.categories) / self.page_size))
        page_info = f"(페이지 {self.page + 1} / {total_pages})"

        if self.mode == "new":
            action = "새 글을 등록할 카테고리를 선택해 주세요."
        elif self.mode == "view":
            action = "조회할 글이 있는 카테고리를 선택해 주세요."
        elif self.mode == "edit":
            action = "수정할 글이 있는 카테고리를 선택해 주세요."
        elif self.mode == "delete":
            action = "삭제할 글이 있는 카테고리를 선택해 주세요."
        elif self.mode == "snapshot_restore":
            action = "스냅샷에서 복원할 글이 있는 카테고리를 선택해 주세요."
        else:
            action = "카테고리를 선택해 주세요."

        return f"📂 {action}\n{page_info}"

    async def _on_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        value = select.values[0]
        if value == "_none":
            await interaction.response.send_message(
                "등록된 카테고리가 없습니다. `/wiki_category_add` 로 먼저 카테고리를 추가해 주세요.",
                ephemeral=True,
            )
            return

        category_name = value

        # 새 글 작성
        if self.mode == "new":
            modal = NewArticleModal(category_name)
            await interaction.response.send_modal(modal)
            return

        # 나머지는 글 목록 조회 필요
        articles = await db_get_articles_in_category(self.guild_id, category_name)
        if not articles:
            await interaction.response.send_message(
                f"`{category_name}` 카테고리에 등록된 글이 없습니다.",
                ephemeral=True,
            )
            return

        art_view = ArticlePickerView(
            mode=self.mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            category_name=category_name,
            articles=articles,
        )
        await interaction.response.edit_message(
            content=art_view.get_header_text(),
            view=art_view,
        )


class CategoryDeleteConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, category_name: str, requester_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.category_name = category_name
        self.requester_id = requester_id

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 확인 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="예", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return

        status, deleted_count = await db_delete_category(
            self.guild_id,
            self.category_name,
            interaction.user.id,
        )

        if status == "no_category":
            await interaction.response.edit_message(
                content="해당 카테고리를 찾을 수 없어 삭제에 실패했습니다.",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=(
                f"⚠️ 카테고리를 삭제할 시 카테고리내에 등록된 모든 정보가 삭제됩니다!\n\n"
                f"✅ `{self.category_name}` 카테고리를 삭제했습니다.\n"
                f"(백업된 글 수: {deleted_count}개)"
            ),
            view=None,
        )

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(
            content="카테고리 삭제를 취소했습니다.",
            view=None,
        )


class CategoryDeletePickerView(discord.ui.View):
    def __init__(self, guild_id: int, requester_id: int, categories: List[asyncpg.Record]):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.categories = categories

        if categories:
            options = [
                discord.SelectOption(
                    label=c["name"],
                    description=(c["description"] or "")[:90] if c["description"] else None,
                    value=c["name"],
                )
                for c in categories
            ]
        else:
            options = [
                discord.SelectOption(
                    label="(카테고리가 없습니다)",
                    value="_none",
                )
            ]

        select = discord.ui.Select(
            placeholder="삭제할 카테고리를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def select_callback(interaction: discord.Interaction):
            await self._on_select(interaction, select)

        select.callback = select_callback
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        value = select.values[0]
        if value == "_none":
            await interaction.response.send_message(
                "삭제할 카테고리가 없습니다.",
                ephemeral=True,
            )
            return

        view = CategoryDeleteConfirmView(
            guild_id=self.guild_id,
            category_name=value,
            requester_id=self.requester_id,
        )
        await interaction.response.send_message(
            "⚠️ 카테고리를 삭제할 시 카테고리내에 등록된 모든 정보가 삭제됩니다!\n\n"
            f"정말로 `{value}` 카테고리를 삭제하시겠습니까?",
            view=view,
            ephemeral=True,
        )
