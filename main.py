import os
from typing import List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands
import asyncpg


# -----------------------------
# 환경 변수 헬퍼
# -----------------------------
def env_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} 환경 변수가 설정되지 않았습니다.")
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} 환경 변수 값이 정수가 아닙니다: {value}")


# -----------------------------
# 환경 변수 / 상수
# -----------------------------
TOKEN = os.getenv("DISCORD_TOKEN")

ALLOWED_GUILD_ID = env_int("ALLOWED_GUILD_ID")        # 허용 서버 ID
WIKI_ADMIN_ROLE_ID = env_int("WIKI_ADMIN_ROLE_ID")    # 삭제/관리 권한 역할 ID
WIKI_EDITOR_ROLE_ID = env_int("WIKI_EDITOR_ROLE_ID")  # 추가/수정/조회 권한 역할 ID

DATABASE_URL = os.getenv("DATABASE_URL")

# 이 길드에만 슬래시 명령어 등록
GUILD_OBJECT = discord.Object(id=ALLOWED_GUILD_ID)

# 기본 카테고리
DEFAULT_CATEGORIES: List[str] = ["공지", "게임", "봇사용법"]


# -----------------------------
# 봇 기본 세팅
# -----------------------------
intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# -----------------------------
# 권한 체크
# -----------------------------
def is_allowed_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.id == ALLOWED_GUILD_ID


def has_wiki_admin_role(interaction: discord.Interaction) -> bool:
    """삭제/관리용 역할"""
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_ADMIN_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_role(interaction: discord.Interaction) -> bool:
    """편집/조회용 역할"""
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_EDITOR_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_or_admin(interaction: discord.Interaction) -> bool:
    """에디터 역할 또는 관리자 역할 둘 중 하나라도 있으면 통과"""
    if not isinstance(interaction.user, discord.Member):
        return False
    role_ids = {role.id for role in interaction.user.roles}
    return (WIKI_EDITOR_ROLE_ID in role_ids) or (WIKI_ADMIN_ROLE_ID in role_ids)


# -----------------------------
# Postgres 연결 풀
# -----------------------------
_db_pool: Optional[asyncpg.pool.Pool] = None


async def get_db_pool() -> asyncpg.pool.Pool:
    global _db_pool
    if _db_pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")
        _db_pool = await asyncpg.create_pool(DATABASE_URL)
    return _db_pool


# -----------------------------
# DB 초기화/마이그레이션
# -----------------------------
async def init_db():
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # 카테고리 테이블
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_categories (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                UNIQUE (guild_id, name)
            );
            """
        )
        # description 컬럼 없으면 추가
        await conn.execute(
            """
            ALTER TABLE wiki_categories
            ADD COLUMN IF NOT EXISTS description TEXT;
            """
        )

        # 글 테이블
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_articles (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                category_id INTEGER NOT NULL REFERENCES wiki_categories(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_by_id BIGINT NOT NULL,
                created_by_name TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (category_id, title)
            );
            """
        )

        # 기여자 테이블
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_contributors (
                article_id INTEGER NOT NULL REFERENCES wiki_articles(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (article_id, user_id)
            );
            """
        )

        # 백업 테이블
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_article_backups (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                article_id INTEGER REFERENCES wiki_articles(id) ON DELETE SET NULL,
                category_name TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_by_id BIGINT,
                created_by_name TEXT,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                op_type TEXT NOT NULL,
                backed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # 기본 카테고리 없으면 넣기
        rows = await conn.fetch(
            "SELECT name FROM wiki_categories WHERE guild_id=$1",
            ALLOWED_GUILD_ID,
        )
        if not rows:
            for name in DEFAULT_CATEGORIES:
                await conn.execute(
                    """
                    INSERT INTO wiki_categories (guild_id, name)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, name) DO NOTHING;
                    """,
                    ALLOWED_GUILD_ID,
                    name,
                )


# -----------------------------
# 카테고리 / 글 관련 DB 함수
# -----------------------------
async def db_get_categories(guild_id: int) -> List[str]:
    """카테고리 이름 리스트만 리턴 (UI용)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM wiki_categories WHERE guild_id=$1 ORDER BY name",
            guild_id,
        )
        if not rows:
            # 해당 길드에 카테고리가 아무것도 없으면 기본 카테고리 생성
            for name in DEFAULT_CATEGORIES:
                await conn.execute(
                    """
                    INSERT INTO wiki_categories (guild_id, name)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, name) DO NOTHING;
                    """,
                    guild_id,
                    name,
                )
            rows = await conn.fetch(
                "SELECT name FROM wiki_categories WHERE guild_id=$1 ORDER BY name",
                guild_id,
            )
        return [r["name"] for r in rows]


async def db_add_category(guild_id: int, name: str, description: Optional[str]) -> bool:
    """카테고리 추가 (이미 있으면 False)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM wiki_categories WHERE guild_id=$1 AND name=$2",
            guild_id,
            name,
        )
        if row:
            return False
        await conn.execute(
            "INSERT INTO wiki_categories (guild_id, name, description) VALUES ($1, $2, $3)",
            guild_id,
            name,
            description,
        )
        return True


async def db_rename_category(guild_id: int, old_name: str, new_name: str) -> Tuple[str, None]:
    """카테고리 이름 변경"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                old_name,
            )
            if not old_row:
                return "no_old", None

            dup_row = await conn.fetchrow(
                "SELECT 1 FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                new_name,
            )
            if dup_row and old_name != new_name:
                return "dup_new", None

            await conn.execute(
                "UPDATE wiki_categories SET name=$1 WHERE id=$2",
                new_name,
                old_row["id"],
            )
            return "ok", None


async def db_delete_category(guild_id: int, name: str) -> Tuple[str, int]:
    """카테고리 삭제 (포함된 글 개수 리턴)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat_row = await conn.fetchrow(
                "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                name,
            )
            if not cat_row:
                return "no_category", 0
            cat_id = cat_row["id"]

            cnt_row = await conn.fetchrow(
                "SELECT COUNT(*) AS c FROM wiki_articles WHERE category_id=$1",
                cat_id,
            )
            deleted_count = cnt_row["c"] if cnt_row else 0

            await conn.execute("DELETE FROM wiki_categories WHERE id=$1", cat_id)
            return "ok", deleted_count


async def db_backup_current_article(conn, article_id: int, op_type: str):
    """현재 글 상태를 백업 테이블에 1건만 보관"""
    art_row = await conn.fetchrow(
        """
        SELECT a.id, a.guild_id, a.title, a.content,
               a.created_by_id, a.created_by_name,
               a.created_at, a.updated_at,
               c.name AS category_name
        FROM wiki_articles a
        JOIN wiki_categories c ON a.category_id = c.id
        WHERE a.id = $1
        """,
        article_id,
    )
    if not art_row:
        return

    # 같은 article_id 에 대한 이전 백업은 삭제 (1개만 유지)
    await conn.execute(
        "DELETE FROM wiki_article_backups WHERE article_id=$1",
        article_id,
    )

    await conn.execute(
        """
        INSERT INTO wiki_article_backups
            (guild_id, article_id, category_name, title, content,
             created_by_id, created_by_name, created_at, updated_at, op_type)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        art_row["guild_id"],
        art_row["id"],
        art_row["category_name"],
        art_row["title"],
        art_row["content"],
        art_row["created_by_id"],
        art_row["created_by_name"],
        art_row["created_at"],
        art_row["updated_at"],
        op_type,
    )


async def db_get_last_backup(guild_id: int):
    """해당 길드 기준으로 가장 최근 백업 1건 조회"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, article_id, category_name, title, content,
                   created_by_id, created_by_name, created_at, updated_at,
                   op_type, backed_at
            FROM wiki_article_backups
            WHERE guild_id=$1
            ORDER BY backed_at DESC
            LIMIT 1
            """,
            guild_id,
        )
        return row


async def db_upsert_article(
    guild_id: int,
    category_name: str,
    title: str,
    content: str,
    user_id: int,
    user_name: str,
):
    """
    새 글 작성 또는 같은 제목이면 내용 덮어쓰기(수정).
    created: 새로 생성이면 True, 수정이면 False
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat_row = await conn.fetchrow(
                "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                category_name,
            )
            if not cat_row:
                raise ValueError("카테고리가 존재하지 않습니다.")
            cat_id = cat_row["id"]

            art_row = await conn.fetchrow(
                """
                SELECT id FROM wiki_articles
                WHERE guild_id=$1 AND category_id=$2 AND title=$3
                """,
                guild_id,
                cat_id,
                title,
            )
            created = False

            if not art_row:
                # 새 글
                art_row = await conn.fetchrow(
                    """
                    INSERT INTO wiki_articles
                        (guild_id, category_id, title, content, created_by_id, created_by_name)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    guild_id,
                    cat_id,
                    title,
                    content,
                    user_id,
                    user_name,
                )
                created = True
            else:
                # 기존 글 수정 → 백업 남기고 업데이트
                article_id = art_row["id"]
                await db_backup_current_article(conn, article_id, "edit")
                await conn.execute(
                    """
                    UPDATE wiki_articles
                    SET content=$1, updated_at=NOW()
                    WHERE id=$2
                    """,
                    content,
                    article_id,
                )

            article_id = art_row["id"]

            # 기여 횟수 +1
            await conn.execute(
                """
                INSERT INTO wiki_contributors (article_id, user_id, count)
                VALUES ($1, $2, 1)
                ON CONFLICT (article_id, user_id)
                DO UPDATE SET count = wiki_contributors.count + 1
                """,
                article_id,
                user_id,
            )

            contrib_row = await conn.fetchrow(
                """
                SELECT count FROM wiki_contributors
                WHERE article_id=$1 AND user_id=$2
                """,
                article_id,
                user_id,
            )
            user_count = contrib_row["count"] if contrib_row else 1

            return created, user_count


async def db_get_article_for_view(
    guild_id: int,
    category_name: str,
    title: str,
):
    """조회용: 글 + 기여자 리스트"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        art_row = await conn.fetchrow(
            """
            SELECT a.id, a.title, a.content,
                   a.created_by_id, a.created_by_name,
                   c.name AS category
            FROM wiki_articles a
            JOIN wiki_categories c ON a.category_id = c.id
            WHERE a.guild_id=$1 AND c.name=$2 AND a.title=$3
            """,
            guild_id,
            category_name,
            title,
        )
        if not art_row:
            return None, None

        contrib_rows = await conn.fetch(
            """
            SELECT user_id, count
            FROM wiki_contributors
            WHERE article_id=$1
            ORDER BY count DESC
            """,
            art_row["id"],
        )

        return art_row, contrib_rows


async def db_get_article_basic(
    guild_id: int,
    category_name: str,
    title: str,
):
    """수정용: 글 기본 정보"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        art_row = await conn.fetchrow(
            """
            SELECT a.id, a.title, a.content,
                   c.name AS category
            FROM wiki_articles a
            JOIN wiki_categories c ON a.category_id = c.id
            WHERE a.guild_id=$1 AND c.name=$2 AND a.title=$3
            """,
            guild_id,
            category_name,
            title,
        )
        return art_row


async def db_edit_article(
    guild_id: int,
    category_name: str,
    old_title: str,
    new_title: str,
    new_content: str,
    user_id: int,
):
    """
    제목 + 내용 수정. 제목 변경 시 중복 체크.
    성공시 ("ok", 기여횟수)
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat_row = await conn.fetchrow(
                "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                category_name,
            )
            if not cat_row:
                return "no_category", None
            cat_id = cat_row["id"]

            art_row = await conn.fetchrow(
                """
                SELECT id, title FROM wiki_articles
                WHERE guild_id=$1 AND category_id=$2 AND title=$3
                """,
                guild_id,
                cat_id,
                old_title,
            )
            if not art_row:
                return "no_article", None

            article_id = art_row["id"]

            # 제목이 바뀐다면 중복 체크
            if new_title != old_title:
                dup_row = await conn.fetchrow(
                    """
                    SELECT 1 FROM wiki_articles
                    WHERE guild_id=$1 AND category_id=$2 AND title=$3
                    """,
                    guild_id,
                    cat_id,
                    new_title,
                )
                if dup_row:
                    return "dup_title", None

            # 백업 저장
            await db_backup_current_article(conn, article_id, "edit")

            # 글 업데이트
            await conn.execute(
                """
                UPDATE wiki_articles
                SET title=$1, content=$2, updated_at=NOW()
                WHERE id=$3
                """,
                new_title,
                new_content,
                article_id,
            )

            # 기여 횟수 +1
            await conn.execute(
                """
                INSERT INTO wiki_contributors (article_id, user_id, count)
                VALUES ($1, $2, 1)
                ON CONFLICT (article_id, user_id)
                DO UPDATE SET count = wiki_contributors.count + 1
                """,
                article_id,
                user_id,
            )
            contrib_row = await conn.fetchrow(
                """
                SELECT count FROM wiki_contributors
                WHERE article_id=$1 AND user_id=$2
                """,
                article_id,
                user_id,
            )
            user_count = contrib_row["count"] if contrib_row else 1

            return "ok", user_count


async def db_delete_article(
    guild_id: int,
    category_name: str,
    title: str,
) -> str:
    """글 삭제 (백업 남기고 삭제)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat_row = await conn.fetchrow(
                "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                category_name,
            )
            if not cat_row:
                return "no_category"
            cat_id = cat_row["id"]

            art_row = await conn.fetchrow(
                """
                SELECT id FROM wiki_articles
                WHERE guild_id=$1 AND category_id=$2 AND title=$3
                """,
                guild_id,
                cat_id,
                title,
            )
            if not art_row:
                return "no_article"

            article_id = art_row["id"]

            # 삭제 전 백업 남기기
            await db_backup_current_article(conn, article_id, "delete")

            await conn.execute(
                "DELETE FROM wiki_articles WHERE id=$1",
                article_id,
            )
            return "ok"


async def db_list_articles_in_category(guild_id: int, category_name: str):
    """카테고리 내 글 목록 (id, title)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.title
            FROM wiki_articles a
            JOIN wiki_categories c ON a.category_id = c.id
            WHERE a.guild_id=$1 AND c.name=$2
            ORDER BY a.title
            """,
            guild_id,
            category_name,
        )
        return rows


def mode_label_kr(mode: str) -> str:
    return {
        "new": "추가",
        "view": "조회",
        "edit": "수정",
        "delete": "삭제",
    }.get(mode, mode)


# -----------------------------
# 검색 결과 UI
# -----------------------------
class SearchResultView(discord.ui.View):
    def __init__(
        self,
        mode: str,
        guild_id: int,
        requester_id: int,
        results: List[asyncpg.Record],
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.results = results

        self.select = SearchResultSelect(self)
        self.add_item(self.select)

    async def handle_article_selected(self, interaction: discord.Interaction, index_str: str):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택지는 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        try:
            idx = int(index_str)
        except ValueError:
            await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
            return

        if idx < 0 or idx >= len(self.results):
            await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
            return

        row = self.results[idx]
        category_name = row["category_name"]
        title = row["title"]

        # 조회
        if self.mode == "view":
            art_row, contrib_rows = await db_get_article_for_view(self.guild_id, category_name, title)
            if not art_row:
                await interaction.response.send_message("해당 글을 찾을 수 없습니다.", ephemeral=True)
                return

            contrib_lines = []
            for cr in contrib_rows:
                contrib_lines.append(f"- <@{cr['user_id']}>: {cr['count']}회")
            contrib_text = "\n".join(contrib_lines) if contrib_lines else "없음"

            embed = discord.Embed(
                title=f"[{art_row['category']}] {art_row['title']}",
                description=art_row["content"],
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="최초 작성자",
                value=f"{art_row['created_by_name']} (<@{art_row['created_by_id']}>)",
                inline=False,
            )
            embed.add_field(
                name="기여자 / 기여 횟수",
                value=contrib_text,
                inline=False,
            )

            await interaction.response.send_message(embed=embed, ephemeral=False)

        # 수정
        elif self.mode == "edit":
            art_row = await db_get_article_basic(self.guild_id, category_name, title)
            if not art_row:
                await interaction.response.send_message("해당 글을 찾을 수 없습니다.", ephemeral=True)
                return

            modal = EditArticleModal(
                guild_id=self.guild_id,
                category=category_name,
                article_id=art_row["id"],
                current_title=art_row["title"],
                current_content=art_row["content"],
            )
            await interaction.response.send_modal(modal)

        # 삭제
        elif self.mode == "delete":
            status = await db_delete_article(self.guild_id, category_name, title)

            if status == "no_category":
                await interaction.response.send_message(
                    f"`{category_name}` 카테고리는 존재하지 않습니다.",
                    ephemeral=True,
                )
                return

            if status == "no_article":
                await interaction.response.send_message(
                    f"[{category_name}] 카테고리에 `{title}` 글이 없습니다.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"🗑️ [{category_name}] `{title}` 글이 삭제되었습니다.",
                ephemeral=True,
            )

        # 기타(안전하게 조회로 처리)
        else:
            art_row, contrib_rows = await db_get_article_for_view(self.guild_id, category_name, title)
            if not art_row:
                await interaction.response.send_message("해당 글을 찾을 수 없습니다.", ephemeral=True)
                return

            contrib_lines = []
            for cr in contrib_rows:
                contrib_lines.append(f"- <@{cr['user_id']}>: {cr['count']}회")
            contrib_text = "\n".join(contrib_lines) if contrib_lines else "없음"

            embed = discord.Embed(
                title=f"[{art_row['category']}] {art_row['title']}",
                description=art_row["content"],
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="최초 작성자",
                value=f"{art_row['created_by_name']} (<@{art_row['created_by_id']}>)",
                inline=False,
            )
            embed.add_field(
                name="기여자 / 기여 횟수",
                value=contrib_text,
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=False)


class SearchResultSelect(discord.ui.Select):
    def __init__(self, parent: SearchResultView):
        self.parent_view = parent

        options: List[discord.SelectOption] = []
        for idx, row in enumerate(parent.results):
            label = f"[{row['category_name']}] {row['title']}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(label=label, value=str(idx)))

        super().__init__(
            placeholder="검색된 글을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.handle_article_selected(interaction, self.values[0])


class SearchModal(discord.ui.Modal):
    """카테고리/제목/내용 전체 검색 모달"""

    def __init__(self, mode: str, guild_id: int, requester_id: int):
        super().__init__(title="위키 검색")
        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id

        self.query_input = discord.ui.TextInput(
            label="검색어",
            placeholder="카테고리 제목 / 글 제목 / 내용 검색",
            max_length=100,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 검색창은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        q = self.query_input.value.strip()
        if not q:
            await interaction.response.send_message(
                "검색어를 입력해 주세요.",
                ephemeral=True,
            )
            return

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.id,
                       c.name AS category_name,
                       a.title
                FROM wiki_articles a
                JOIN wiki_categories c ON a.category_id = c.id
                WHERE a.guild_id=$1
                  AND (
                    c.name ILIKE '%' || $2 || '%' OR
                    a.title ILIKE '%' || $2 || '%' OR
                    a.content ILIKE '%' || $2 || '%'
                  )
                ORDER BY a.updated_at DESC
                LIMIT 10
                """,
                self.guild_id,
                q,
            )

        if not rows:
            await interaction.response.send_message(
                "검색 결과가 없습니다.",
                ephemeral=True,
            )
            return

        view = SearchResultView(
            mode=self.mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            results=rows,
        )

        lines = [f"- [{r['category_name']}] {r['title']}" for r in rows]
        text = "🔍 검색 결과 (최대 10개):\n" + "\n".join(lines) + "\n\n열람/수정/삭제할 글을 선택해 주세요."

        await interaction.response.send_message(
            text,
            view=view,
            ephemeral=True,
        )


# -----------------------------
# 글 작성 / 수정 모달
# -----------------------------
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
            created, contrib_count = await db_upsert_article(
                guild.id,
                self.category,
                title,
                content,
                user.id,
                getattr(user, "display_name", str(user)),
            )
        except ValueError:
            await interaction.response.send_message(
                "선택한 카테고리가 존재하지 않습니다. /wiki_category_add 로 먼저 카테고리를 만들어 주세요.",
                ephemeral=True,
            )
            return

        msg = "새 글이 등록되었습니다." if created else "기존 글이 수정되었습니다."

        await interaction.response.send_message(
            f"✅ [{self.category}] `{title}` 저장 완료! ({msg})\n"
            f"작성/수정자: {user.mention} (이 글에 {contrib_count}번째 기여)",
            ephemeral=True,
        )


class EditArticleModal(discord.ui.Modal):
    """제목 + 내용 수정 모달"""

    def __init__(
        self,
        guild_id: int,
        category: str,
        article_id: int,
        current_title: str,
        current_content: str,
    ):
        super().__init__(title=f"[{category}] 글 수정: {current_title}")
        self.guild_id = guild_id
        self.category = category
        self.article_id = article_id
        self.old_title = current_title

        self.title_input = discord.ui.TextInput(
            label="제목",
            max_length=100,
            default=current_title,
        )
        self.content_input = discord.ui.TextInput(
            label="내용",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            default=current_content,
        )

        self.add_item(self.title_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "길드 정보가 일치하지 않습니다.",
                ephemeral=True,
            )
            return

        new_title = self.title_input.value.strip()
        new_content = self.content_input.value.strip()

        if not new_title or not new_content:
            await interaction.response.send_message(
                "제목과 내용은 비어 있을 수 없습니다.",
                ephemeral=True,
            )
            return

        status, contrib_count = await db_edit_article(
            guild.id,
            self.category,
            self.old_title,
            new_title,
            new_content,
            user.id,
        )

        if status == "no_category":
            await interaction.response.send_message(
                f"`{self.category}` 카테고리가 존재하지 않습니다.",
                ephemeral=True,
            )
            return

        if status == "no_article":
            await interaction.response.send_message(
                f"[{self.category}] 카테고리에 `{self.old_title}` 글이 더 이상 존재하지 않습니다.",
                ephemeral=True,
            )
            return

        if status == "dup_title":
            await interaction.response.send_message(
                f"같은 카테고리에 이미 `{new_title}` 제목의 글이 있습니다. 다른 제목을 사용해 주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✏️ `{new_title}` 글이 수정되었습니다.\n"
            f"{user.mention} 이(가) 이 글에 {contrib_count}번째 기여를 했습니다.",
            ephemeral=True,
        )


# -----------------------------
# 카테고리 선택 View (페이지 + 검색)
# -----------------------------
class CategorySelect(discord.ui.Select):
    def __init__(self, parent_view: "CategoryPickerView"):
        self.parent_view = parent_view
        super().__init__(
            placeholder="카테고리를 선택하세요.",
            min_values=1,
            max_values=1,
            options=[],
        )
        self.update_options()

    def update_options(self):
        cats = self.parent_view.categories
        per_page = self.parent_view.per_page
        page = self.parent_view.page

        start = page * per_page
        end = start + per_page
        slice_items = cats[start:end]

        if not slice_items:
            self.options = [
                discord.SelectOption(label="(카테고리 없음)", value="__none__")
            ]
            self.disabled = True
        else:
            self.options = [
                discord.SelectOption(label=name, value=name)
                for name in slice_items
            ]
            self.disabled = False

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.handle_category_selected(interaction, self.values[0])


class CategoryPickerView(discord.ui.View):
    """카테고리 리스트 (10개씩 페이지) + 검색 버튼"""

    def __init__(
        self,
        mode: str,  # "new" / "view" / "edit" / "delete"
        guild_id: int,
        requester_id: int,
        categories: List[str],
        per_page: int = 10,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.categories = categories
        self.per_page = per_page
        self.page = 0

        self.category_select = CategorySelect(self)
        self.add_item(self.category_select)

    async def handle_category_selected(self, interaction: discord.Interaction, value: str):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택지는 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        if value == "__none__":
            await interaction.response.send_message(
                "현재 선택할 수 있는 카테고리가 없습니다.",
                ephemeral=True,
            )
            return

        category_name = value

        # 새 글 작성 모드: 카테고리 선택 후 바로 모달
        if self.mode == "new":
            await interaction.response.send_modal(NewArticleModal(category_name))
            return

        # 조회/수정/삭제 모드: 카테고리 내 글 목록 View 로 전환
        articles = await db_list_articles_in_category(self.guild_id, category_name)

        if not articles:
            await interaction.response.send_message(
                f"[{category_name}] 카테고리에 등록된 글이 없습니다.",
                ephemeral=True,
            )
            return

        view = ArticlePickerView(
            mode=self.mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            category_name=category_name,
            articles=articles,
        )
        label = mode_label_kr(self.mode)

        await interaction.response.edit_message(
            content=f"📄 [{category_name}] 카테고리에서 {label}할 글을 선택하세요.",
            view=view,
        )

    @discord.ui.button(label="이전 페이지", style=discord.ButtonStyle.secondary, row=4)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        if self.page > 0:
            self.page -= 1
            self.category_select.update_options()

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="다음 페이지", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        max_page = 0
        if self.categories:
            max_page = (len(self.categories) - 1) // self.per_page

        if self.page < max_page:
            self.page += 1
            self.category_select.update_options()

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="검색", style=discord.ButtonStyle.primary, row=4)
    async def open_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        # /wiki_new 에서도 검색을 누르면 그냥 조회 모드처럼 동작하게 처리
        effective_mode = self.mode if self.mode in ("view", "edit", "delete") else "view"

        modal = SearchModal(
            mode=effective_mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
        )
        await interaction.response.send_modal(modal)


# -----------------------------
# 글 선택 View (카테고리 안에서 10개씩 페이지) + 검색
# -----------------------------
class ArticleSelect(discord.ui.Select):
    def __init__(self, parent_view: "ArticlePickerView"):
        self.parent_view = parent_view
        super().__init__(
            placeholder="글을 선택하세요.",
            min_values=1,
            max_values=1,
            options=[],
        )
        self.update_options()

    def update_options(self):
        arts = self.parent_view.articles
        per_page = self.parent_view.per_page
        page = self.parent_view.page

        start = page * per_page
        end = start + per_page
        slice_items = list(enumerate(arts))[start:end]

        if not slice_items:
            self.options = [
                discord.SelectOption(label="(글 없음)", value="__none__")
            ]
            self.disabled = True
        else:
            opts: List[discord.SelectOption] = []
            for idx, row in slice_items:
                label = row["title"]
                if len(label) > 100:
                    label = label[:97] + "..."
                opts.append(discord.SelectOption(label=label, value=str(idx)))
            self.options = opts
            self.disabled = False

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.handle_article_selected(interaction, self.values[0])


class ArticlePickerView(discord.ui.View):
    def __init__(
        self,
        mode: str,
        guild_id: int,
        requester_id: int,
        category_name: str,
        articles: List[asyncpg.Record],
        per_page: int = 10,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.mode = mode
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.category_name = category_name
        self.articles = articles
        self.per_page = per_page
        self.page = 0

        self.article_select = ArticleSelect(self)
        self.add_item(self.article_select)

    async def handle_article_selected(self, interaction: discord.Interaction, value: str):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 선택지는 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        if value == "__none__":
            await interaction.response.send_message(
                "현재 선택할 수 있는 글이 없습니다.",
                ephemeral=True,
            )
            return

        try:
            idx = int(value)
        except ValueError:
            await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
            return

        if idx < 0 or idx >= len(self.articles):
            await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
            return

        row = self.articles[idx]
        title = row["title"]
        category = self.category_name

        # 조회
        if self.mode == "view":
            art_row, contrib_rows = await db_get_article_for_view(self.guild_id, category, title)
            if not art_row:
                await interaction.response.send_message("해당 글을 찾을 수 없습니다.", ephemeral=True)
                return

            contrib_lines = []
            for cr in contrib_rows:
                contrib_lines.append(f"- <@{cr['user_id']}>: {cr['count']}회")
            contrib_text = "\n".join(contrib_lines) if contrib_lines else "없음"

            embed = discord.Embed(
                title=f"[{art_row['category']}] {art_row['title']}",
                description=art_row["content"],
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="최초 작성자",
                value=f"{art_row['created_by_name']} (<@{art_row['created_by_id']}>)",
                inline=False,
            )
            embed.add_field(
                name="기여자 / 기여 횟수",
                value=contrib_text,
                inline=False,
            )

            await interaction.response.send_message(embed=embed, ephemeral=False)

        # 수정
        elif self.mode == "edit":
            art_row = await db_get_article_basic(self.guild_id, category, title)
            if not art_row:
                await interaction.response.send_message("해당 글을 찾을 수 없습니다.", ephemeral=True)
                return

            modal = EditArticleModal(
                guild_id=self.guild_id,
                category=category,
                article_id=art_row["id"],
                current_title=art_row["title"],
                current_content=art_row["content"],
            )
            await interaction.response.send_modal(modal)

        # 삭제
        elif self.mode == "delete":
            status = await db_delete_article(self.guild_id, category, title)

            if status == "no_category":
                await interaction.response.send_message(
                    f"`{category}` 카테고리는 존재하지 않습니다.",
                    ephemeral=True,
                )
                return

            if status == "no_article":
                await interaction.response.send_message(
                    f"[{category}] 카테고리에 `{title}` 글이 없습니다.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"🗑️ [{category}] `{title}` 글이 삭제되었습니다.",
                ephemeral=True,
            )

        else:
            await interaction.response.send_message(
                "알 수 없는 동작 모드입니다.",
                ephemeral=True,
            )

    @discord.ui.button(label="이전 페이지", style=discord.ButtonStyle.secondary, row=4)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        if self.page > 0:
            self.page -= 1
            self.article_select.update_options()

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="다음 페이지", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        max_page = 0
        if self.articles:
            max_page = (len(self.articles) - 1) // self.per_page

        if self.page < max_page:
            self.page += 1
            self.article_select.update_options()

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="검색", style=discord.ButtonStyle.primary, row=4)
    async def open_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        effective_mode = self.mode if self.mode in ("view", "edit", "delete") else "view"

        modal = SearchModal(
            mode=effective_mode,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
        )
        await interaction.response.send_modal(modal)


# -----------------------------
# 백업 복원 View
# -----------------------------
class RestoreBackupView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        backup_id: int,
        category_name: str,
        title: str,
        content: str,
        created_by_id: Optional[int],
        created_by_name: Optional[str],
        created_at,
        updated_at,
        op_type: str,
        article_id: Optional[int],
        requester_id: int,
        timeout: float = 60.0,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.backup_id = backup_id
        self.category_name = category_name
        self.title = title
        self.content = content
        self.created_by_id = created_by_id
        self.created_by_name = created_by_name
        self.created_at = created_at
        self.updated_at = updated_at
        self.op_type = op_type
        self.article_id = article_id
        self.requester_id = requester_id

    async def _restore(self, interaction: discord.Interaction):
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 카테고리가 아직 존재하는지 확인
                cat_row = await conn.fetchrow(
                    "SELECT id FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                    self.guild_id,
                    self.category_name,
                )
                if not cat_row:
                    await interaction.response.send_message(
                        f"백업된 카테고리 `{self.category_name}` 가(이) 이미 삭제되어 복원할 수 없습니다.",
                        ephemeral=True,
                    )
                    return

                category_id = cat_row["id"]

                # 수정 백업인지, 삭제 백업인지에 따라 처리
                if self.op_type == "edit":
                    if self.article_id is not None:
                        art_row = await conn.fetchrow(
                            "SELECT id FROM wiki_articles WHERE id=$1",
                            self.article_id,
                        )
                    else:
                        # article_id 가 없으면 제목/카테고리로 찾기
                        art_row = await conn.fetchrow(
                            """
                            SELECT a.id
                            FROM wiki_articles a
                            JOIN wiki_categories c ON a.category_id = c.id
                            WHERE a.guild_id=$1 AND c.id=$2 AND a.title=$3
                            """,
                            self.guild_id,
                            category_id,
                            self.title,
                        )

                    if not art_row:
                        # 글이 사라졌으면 새로 생성
                        await conn.execute(
                            """
                            INSERT INTO wiki_articles
                                (guild_id, category_id, title, content,
                                 created_by_id, created_by_name,
                                 created_at, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6,COALESCE($7,NOW()),NOW())
                            """,
                            self.guild_id,
                            category_id,
                            self.title,
                            self.content,
                            self.created_by_id or interaction.user.id,
                            self.created_by_name or getattr(interaction.user, "display_name", str(interaction.user)),
                            self.created_at,
                        )
                    else:
                        # 기존 글 덮어쓰기
                        await conn.execute(
                            """
                            UPDATE wiki_articles
                            SET title=$1, content=$2, category_id=$3, updated_at=NOW()
                            WHERE id=$4
                            """,
                            self.title,
                            self.content,
                            category_id,
                            art_row["id"],
                        )

                else:
                    # 삭제 백업 → 새로 생성
                    await conn.execute(
                        """
                        INSERT INTO wiki_articles
                            (guild_id, category_id, title, content,
                             created_by_id, created_by_name,
                             created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,COALESCE($7,NOW()),NOW())
                        """,
                        self.guild_id,
                        category_id,
                        self.title,
                        self.content,
                        self.created_by_id or interaction.user.id,
                        self.created_by_name or getattr(interaction.user, "display_name", str(interaction.user)),
                        self.created_at,
                    )

        await interaction.response.send_message(
            f"✅ [{self.category_name}] `{self.title}` 글을 직전 상태으로 복원했습니다.",
            ephemeral=True,
        )

    @discord.ui.button(label="예", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        await self._restore(interaction)

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 버튼은 이 명령어를 실행한 사용자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "복원을 취소했습니다. 백업 데이터는 그대로 유지됩니다.",
            ephemeral=True,
        )


# -----------------------------
# /wiki_backup_restore : 직전 상태로 되돌리기
# -----------------------------
@bot.tree.command(
    name="wiki_backup_restore",
    description="직전에 수정/삭제된 위키 글을 직전 상태로 되돌립니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
async def wiki_backup_restore(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    backup = await db_get_last_backup(guild.id)
    if not backup:
        await interaction.response.send_message(
            "되돌릴 수 있는 백업 데이터가 없습니다.",
            ephemeral=True,
        )
        return

    category_name = backup["category_name"]
    title = backup["title"]
    op_type = backup["op_type"]
    msg_type = "수정" if op_type == "edit" else "삭제"

    text = (
        f"📦 직전 백업 정보\n"
        f"- 작업 종류: `{msg_type}`\n"
        f"- 카테고리: `{category_name}`\n"
        f"- 제목: `{title}`\n\n"
        "해당 정보를 직전 상태로 되돌리겠습니까?"
    )

    view = RestoreBackupView(
        guild_id=guild.id,
        backup_id=backup["id"],
        category_name=category_name,
        title=title,
        content=backup["content"],
        created_by_id=backup["created_by_id"],
        created_by_name=backup["created_by_name"],
        created_at=backup["created_at"],
        updated_at=backup["updated_at"],
        op_type=op_type,
        article_id=backup["article_id"],
        requester_id=interaction.user.id,
    )

    await interaction.response.send_message(
        text,
        view=view,
        ephemeral=True,
    )


# -----------------------------
# 카테고리 관리 명령어
# -----------------------------
@bot.tree.command(
    name="wiki_category_list",
    description="위키 카테고리 목록을 보여줍니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
async def wiki_category_list(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description
            FROM wiki_categories
            WHERE guild_id=$1
            ORDER BY name
            """,
            guild.id,
        )

        if not rows:
            await interaction.response.send_message(
                "현재 등록된 카테고리가 없습니다.",
                ephemeral=True,
            )
            return

        lines = []
        for r in rows:
            cnt_row = await conn.fetchrow(
                "SELECT COUNT(*) AS c FROM wiki_articles WHERE category_id=$1",
                r["id"],
            )
            count = cnt_row["c"] if cnt_row else 0
            line = f"- `{r['name']}`"
            if r["description"]:
                line += f" — {r['description']}"
            line += f" ({count}개 글)"
            lines.append(line)

    text = "📂 현재 카테고리 목록:\n" + "\n".join(lines)

    await interaction.response.send_message(
        text,
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_category_add",
    description="새 위키 카테고리를 추가합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
@app_commands.describe(
    name="추가할 카테고리 이름",
    description="카테고리 비고/설명 (선택)",
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

    name = name.strip()
    if not name:
        await interaction.response.send_message(
            "카테고리 이름은 비어 있을 수 없습니다.",
            ephemeral=True,
        )
        return

    if len(name) > 25:
        await interaction.response.send_message(
            "카테고리 이름은 25자를 넘을 수 없습니다.",
            ephemeral=True,
        )
        return

    desc = description.strip() if description else None

    created = await db_add_category(guild.id, name, desc)
    if not created:
        await interaction.response.send_message(
            f"이미 `{name}` 카테고리가 존재합니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ 카테고리 `{name}` 이(가) 추가되었습니다.",
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_category_rename",
    description="기존 카테고리 이름을 변경합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
@app_commands.describe(
    old_name="변경할 기존 카테고리 이름",
    new_name="새 카테고리 이름",
)
async def wiki_category_rename(
    interaction: discord.Interaction,
    old_name: str,
    new_name: str,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    old_name = old_name.strip()
    new_name = new_name.strip()

    if not new_name:
        await interaction.response.send_message(
            "새 카테고리 이름은 비어 있을 수 없습니다.",
            ephemeral=True,
        )
        return

    status, _ = await db_rename_category(guild.id, old_name, new_name)

    if status == "no_old":
        await interaction.response.send_message(
            f"`{old_name}` 카테고리는 존재하지 않습니다.",
            ephemeral=True,
        )
        return

    if status == "dup_new":
        await interaction.response.send_message(
            f"`{new_name}` 이름의 카테고리가 이미 존재합니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✏️ 카테고리 `{old_name}` → `{new_name}` 으로 변경되었습니다.",
        ephemeral=True,
    )


@bot.tree.command(
    name="wiki_category_delete",
    description="카테고리를 삭제합니다 (해당 카테고리 글도 모두 삭제).",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
@app_commands.describe(name="삭제할 카테고리 이름")
async def wiki_category_delete(interaction: discord.Interaction, name: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "길드 안에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return

    name = name.strip()

    status, deleted_count = await db_delete_category(guild.id, name)

    if status == "no_category":
        await interaction.response.send_message(
            f"`{name}` 카테고리는 존재하지 않습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🗑️ 카테고리 `{name}` 를 삭제했습니다. (포함된 글 {deleted_count}개도 함께 삭제)",
        ephemeral=True,
    )


# -----------------------------
# 글 추가/조회/수정/삭제 슬래시 명령어
# -----------------------------
@bot.tree.command(
    name="wiki_new",
    description="위키에 새 글을 등록합니다.",
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

    categories = await db_get_categories(guild.id)

    view = CategoryPickerView(
        mode="new",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )

    await interaction.response.send_message(
        "📚 새 글을 등록할 카테고리를 선택해 주세요.",
        view=view,
        ephemeral=True,
    )


@wiki_new.error
async def wiki_new_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "이 명령어를 사용할 권한이 없거나, 이 봇은 지정된 서버에서만 사용할 수 없습니다.",
            ephemeral=True,
        )


@bot.tree.command(
    name="wiki_view",
    description="위키 글을 조회합니다.",
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

    categories = await db_get_categories(guild.id)

    view = CategoryPickerView(
        mode="view",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )

    await interaction.response.send_message(
        "🔍 조회할 카테고리를 선택하거나, 아래 검색 버튼으로 검색해 주세요.",
        view=view,
        ephemeral=True,
    )


@wiki_view.error
async def wiki_view_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "이 명령어를 사용할 권한이 없거나, 이 봇은 지정된 서버에서만 사용할 수 없습니다.",
            ephemeral=True,
        )


@bot.tree.command(
    name="wiki_edit",
    description="위키 글을 수정합니다.",
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

    categories = await db_get_categories(guild.id)

    view = CategoryPickerView(
        mode="edit",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )

    await interaction.response.send_message(
        "✏️ 수정할 글이 있는 카테고리를 선택하거나, 아래 검색 버튼으로 검색해 주세요.",
        view=view,
        ephemeral=True,
    )


@wiki_edit.error
async def wiki_edit_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "이 명령어를 사용할 권한이 없거나, 이 봇은 지정된 서버에서만 사용할 수 없습니다.",
            ephemeral=True,
        )


@bot.tree.command(
    name="wiki_delete",
    description="위키 글을 삭제합니다.",
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

    categories = await db_get_categories(guild.id)

    view = CategoryPickerView(
        mode="delete",
        guild_id=guild.id,
        requester_id=interaction.user.id,
        categories=categories,
    )

    await interaction.response.send_message(
        "🗑️ 삭제할 글이 있는 카테고리를 선택하거나, 아래 검색 버튼으로 검색해 주세요.",
        view=view,
        ephemeral=True,
    )


@wiki_delete.error
async def wiki_delete_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "삭제 권한이 없거나, 이 봇이 동작하도록 허용된 서버가 아닙니다.",
            ephemeral=True,
        )


# -----------------------------
# on_ready: DB 초기화 + 슬래시 명령어 동기화
# -----------------------------
@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")

    try:
        await init_db()
        print("✅ DB 초기화/마이그레이션 완료")
    except Exception as e:
        print("❌ DB 초기화 중 오류:", e)
        return

    try:
        synced = await bot.tree.sync(guild=GUILD_OBJECT)
        print(f"✅ 슬래시 명령어 {len(synced)}개 길드 동기화 완료 (guild_id={ALLOWED_GUILD_ID})")
        print("✅ 봇 준비 완료 & 슬래시 명령어 동기화 완료")
    except Exception as e:
        print("❌ 슬래시 명령어 동기화 중 오류:", e)


# -----------------------------
# 메인 실행
# -----------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN 환경 변수를 설정해 주세요.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경 변수를 설정해 주세요.")
    bot.run(TOKEN)
