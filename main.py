import os
import re
import math
import asyncio
from typing import Optional, List, Tuple
from urllib.parse import urlsplit

import asyncpg
import discord
from discord.ext import commands, tasks
from discord import app_commands

# =============================
# 환경 변수 헬퍼
# =============================

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

# =============================
# 디스코드 봇 기본 세팅
# =============================

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =============================
# 권한 체크 함수
# =============================

def is_allowed_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.id == ALLOWED_GUILD_ID


def has_wiki_admin_role(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_ADMIN_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_role(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_EDITOR_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_or_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    role_ids = {role.id for role in interaction.user.roles}
    return (WIKI_EDITOR_ROLE_ID in role_ids) or (WIKI_ADMIN_ROLE_ID in role_ids)

# =============================
# DB 풀 + 초기화
# =============================

DB_POOL: Optional[asyncpg.Pool] = None
DB_LOCK = asyncio.Lock()


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        # 카테고리 테이블
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_categories (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, name)
            );
            """
        )

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
                created_by_id BIGINT,
                created_by_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, category_id, title)
            );
            """
        )

        await conn.execute(
            """
            ALTER TABLE wiki_articles
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
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
                actor_id BIGINT,
                backed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            ALTER TABLE wiki_article_backups
            ADD COLUMN IF NOT EXISTS actor_id BIGINT;
            """
        )


async def get_db_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        async with DB_LOCK:
            if DB_POOL is None:
                DB_POOL = await asyncpg.create_pool(DATABASE_URL)
                await init_db(DB_POOL)
    return DB_POOL

# =============================
# DB 헬퍼 함수 (카테고리)
# =============================

async def db_get_all_categories(guild_id: int) -> List[asyncpg.Record]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description
            FROM wiki_categories
            WHERE guild_id=$1
            ORDER BY name
            """,
            guild_id,
        )
        return rows


async def db_add_category(guild_id: int, name: str, description: Optional[str]) -> str:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchrow(
                "SELECT 1 FROM wiki_categories WHERE guild_id=$1 AND name=$2",
                guild_id,
                name,
            )
            if exists:
                return "dup"

            await conn.execute(
                """
                INSERT INTO wiki_categories (guild_id, name, description)
                VALUES ($1, $2, $3)
                """,
                guild_id,
                name,
                description,
            )
            return "ok"


async def db_backup_current_article(conn: asyncpg.Connection, article_id: int, op_type: str, actor_id: int):
    """
    현재 글 상태를 백업 테이블에 저장.
    - 동일 article_id + actor_id 조합의 직전 백업은 삭제하고 새로 1건만 유지
    """
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

    await conn.execute(
        "DELETE FROM wiki_article_backups WHERE article_id=$1 AND actor_id=$2",
        article_id,
        actor_id,
    )

    await conn.execute(
        """
        INSERT INTO wiki_article_backups
            (guild_id, article_id, category_name, title, content,
             created_by_id, created_by_name, created_at, updated_at,
             op_type, actor_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
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
        actor_id,
    )


async def db_delete_category(guild_id: int, name: str, actor_id: int) -> Tuple[str, int]:
    """
    카테고리 삭제 (포함된 글 전체 백업 후 삭제)
    return: (status, 삭제된 글 수)
    """
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

            art_rows = await conn.fetch(
                "SELECT id FROM wiki_articles WHERE category_id=$1",
                cat_id,
            )

            for ar in art_rows:
                await db_backup_current_article(conn, ar["id"], "delete", actor_id)

            deleted_count = len(art_rows)

            await conn.execute("DELETE FROM wiki_categories WHERE id=$1", cat_id)
            return "ok", deleted_count

# =============================
# DB 헬퍼 함수 (백업 조회)
# =============================

async def db_get_last_backup_for_user(guild_id: int, user_id: int) -> Optional[asyncpg.Record]:
    """
    해당 길드 + 해당 유저 기준으로 가장 최근 백업 1건 조회
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, article_id, category_name, title, content,
                   created_by_id, created_by_name, created_at, updated_at,
                   op_type, backed_at, actor_id
            FROM wiki_article_backups
            WHERE guild_id=$1 AND actor_id=$2
            ORDER BY backed_at DESC
            LIMIT 1
            """,
            guild_id,
            user_id,
        )
        return row

# =============================
# DB 헬퍼 함수 (글)
# =============================

async def db_upsert_article(
    guild_id: int,
    category_name: str,
    title: str,
    content: str,
    user_id: int,
    user_name: str,
):
    """
    새 글 '생성 전용' 함수.
    - 동일 카테고리 + 제목이 이미 있으면 아무 것도 변경하지 않고 "dup" 반환
    - 성공 시 ("created", 기여횟수=1) 반환
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

            existing = await conn.fetchrow(
                """
                SELECT id FROM wiki_articles
                WHERE guild_id=$1 AND category_id=$2 AND title=$3
                """,
                guild_id,
                cat_id,
                title,
            )
            if existing:
                return "dup", None

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
            article_id = art_row["id"]

            await conn.execute(
                """
                INSERT INTO wiki_contributors (article_id, user_id, count)
                VALUES ($1, $2, 1)
                """,
                article_id,
                user_id,
            )

            return "created", 1


async def db_get_articles_in_category(guild_id: int, category_name: str) -> List[asyncpg.Record]:
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


async def db_get_article_for_view(
    guild_id: int,
    category_name: str,
    title: str,
) -> Tuple[Optional[asyncpg.Record], List[asyncpg.Record]]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        art_row = await conn.fetchrow(
            """
            SELECT a.id, a.guild_id, a.title, a.content,
                   a.created_by_id, a.created_by_name,
                   a.created_at, a.updated_at,
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
            return None, []

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

            await db_backup_current_article(conn, article_id, "edit", user_id)

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
    actor_id: int,
) -> str:
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

            await db_backup_current_article(conn, article_id, "delete", actor_id)

            await conn.execute("DELETE FROM wiki_articles WHERE id=$1", article_id)
            return "ok"


async def db_search_articles(guild_id: int, query: str, limit: int = 10) -> List[asyncpg.Record]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        pattern = f"%{query}%"
        rows = await conn.fetch(
            """
            SELECT c.name AS category_name, a.title
            FROM wiki_articles a
            JOIN wiki_categories c ON a.category_id = c.id
            WHERE a.guild_id=$1
              AND (c.name ILIKE $2 OR a.title ILIKE $2 OR a.content ILIKE $2)
            ORDER BY a.id DESC
            LIMIT $3
            """,
            guild_id,
            pattern,
            limit,
        )
        return rows

# =============================
# 백업 정리(최적화) 작업
# =============================

async def compact_backups_once():
    """
    - article_id 가 살아있는 백업들은 현재 wiki_articles 내용으로 동기화
    - article_id 가 NULL 인(= 실제 글이 이미 삭제된) 백업들은 삭제
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                """
                UPDATE wiki_article_backups AS b
                SET category_name   = c.name,
                    title           = a.title,
                    content         = a.content,
                    created_by_id   = a.created_by_id,
                    created_by_name = a.created_by_name,
                    created_at      = a.created_at,
                    updated_at      = a.updated_at
                FROM wiki_articles AS a
                JOIN wiki_categories AS c
                  ON c.id = a.category_id
                WHERE b.article_id = a.id
                  AND b.guild_id   = a.guild_id;
                """
            )

            deleted = await conn.execute(
                """
                DELETE FROM wiki_article_backups
                WHERE article_id IS NULL;
                """
            )

    print(f"⏱️ 백업 정리 1회 실행 완료. 결과: {updated}, {deleted}")


@tasks.loop(hours=24)
async def backup_maintenance_task():
    """
    24시간 간격으로 백업 정리 작업 실행
    """
    try:
        await compact_backups_once()
    except Exception as e:
        print("❌ 백업 정리 작업 중 오류:", e)


@backup_maintenance_task.before_loop
async def before_backup_maintenance_task():
    await bot.wait_until_ready()
    print("⏱️ 백업 정리 작업 대기 완료. 봇 준비 후 24시간 간격으로 실행됩니다.")

# =============================
# 이미지 URL 처리 + Embed 생성
# =============================

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def split_content_and_images(content: str) -> Tuple[str, List[str]]:
    """
    내용 문자열 안에서 여러 이미지 URL을 찾아:
    - 내용에서는 각 이미지 URL을 '[이미지1]', '[이미지2]' ... 로 치환하고
    - 이미지 URL 리스트를 순서대로 반환한다.
    """
    image_urls: List[str] = []
    index = 0

    def repl(match):
        nonlocal index, image_urls
        raw_url = match.group(0)

        cleaned = raw_url.strip(".,);>\"'&").strip("<>")

        parsed = urlsplit(cleaned)
        path_lower = parsed.path.lower()

        if any(path_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
            index += 1
            image_urls.append(cleaned)
            return f"[이미지{index}]"
        else:
            return raw_url

    cleaned_content = re.sub(r"(https?://\S+)", repl, content)
    return cleaned_content, image_urls


def build_article_embeds(
    art_row: asyncpg.Record,
    contrib_rows: List[asyncpg.Record],
) -> List[discord.Embed]:
    """
    글 1개를 여러 Embed로 분리해서 반환:
    - 첫 번째 Embed: 텍스트(본문) + 작성자/기여자 정보
    - 이후 Embed들: 이미지 전용 embed (이미지 개수만큼, 제한 없음)
    """
    cleaned_content, image_urls = split_content_and_images(art_row["content"])

    contrib_lines = [
        f"- <@{cr['user_id']}>: {cr['count']}회" for cr in contrib_rows
    ]
    contrib_text = "\n".join(contrib_lines) if contrib_lines else "없음"

    main_embed = discord.Embed(
        title=f"[{art_row['category']}] {art_row['title']}",
        description=cleaned_content,
        color=discord.Color.blurple(),
    )
    main_embed.add_field(
        name="최초 작성자",
        value=f"{art_row['created_by_name']} (<@{art_row['created_by_id']}>)",
        inline=False,
    )
    main_embed.add_field(
        name="기여자 / 기여 횟수",
        value=contrib_text,
        inline=False,
    )

    embeds: List[discord.Embed] = [main_embed]

    for idx, url in enumerate(image_urls):
        img_embed = discord.Embed(color=discord.Color.blurple())
        img_embed.set_image(url=url)
        img_embed.set_footer(text=f"이미지 {idx + 1}")
        embeds.append(img_embed)

    return embeds


async def send_embeds_with_chunking(
    interaction: discord.Interaction,
    embeds: List[discord.Embed],
    ephemeral: bool = False,
):
    """
    디스코드 제한(메시지당 최대 10개 embed)을 고려하여
    여러 번의 메시지로 나누어 embed들을 전송한다.
    """
    if not embeds:
        return

    MAX_EMBEDS = 10
    first_chunk = embeds[:MAX_EMBEDS]
    await interaction.response.send_message(embeds=first_chunk, ephemeral=ephemeral)

    remaining = embeds[MAX_EMBEDS:]
    for i in range(0, len(remaining), MAX_EMBEDS):
        chunk = remaining[i : i + MAX_EMBEDS]
        await interaction.followup.send(embeds=chunk, ephemeral=ephemeral)

# =============================
# UI: 새 글 작성 모달
# =============================

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

# =============================
# UI: 글 수정 모달 + 확인 뷰
# =============================

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

# =============================
# UI: 삭제 확인 뷰
# =============================

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

# =============================
# UI: 백업 복구 뷰
# =============================

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

# =============================
# UI: 검색 모달 + 결과 뷰
# =============================

class SearchModal(discord.ui.Modal):
    def __init__(self, mode: str, guild_id: int, requester_id: int):
        title_map = {
            "view": "위키 검색 (조회)",
            "edit": "위키 검색 (수정)",
            "delete": "위키 검색 (삭제)",
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

        lines = [f"- [{r['category_name']}] {r['title']}" for r in rows]

        if self.mode == "view":
            action_text = "조회할 글을 선택해 주세요."
        elif self.mode == "edit":
            action_text = "수정할 글을 선택해 주세요."
        elif self.mode == "delete":
            action_text = "삭제할 글을 선택해 주세요."
        else:
            action_text = "처리할 글을 선택해 주세요."

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

# =============================
# UI: 카테고리/글 선택 뷰 (페이지네이션)
# =============================

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
        self.mode = mode  # "view" / "edit" / "delete"
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
        self.mode = mode  # "new" / "view" / "edit" / "delete"
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

        if self.mode == "new":
            modal = NewArticleModal(category_name)
            await interaction.response.send_modal(modal)
            return

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

# =============================
# UI: 카테고리 삭제 선택 뷰
# =============================

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
            f"⚠️ 카테고리를 삭제할 시 카테고리내에 등록된 모든 정보가 삭제됩니다!\n\n"
            f"정말로 `{value}` 카테고리를 삭제하시겠습니까?",
            view=view,
            ephemeral=True,
        )

# =============================
# Slash 명령어들
# =============================

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
@app_commands.check(has_wiki_admin_role)
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
    description="카테고리를 삭제합니다. (안의 글도 모두 함께 삭제)",
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
    description="직전에 수정/삭제했던 내용을 되돌립니다.",
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

    backup = await db_get_last_backup_for_user(guild.id, interaction.user.id)
    if not backup:
        await interaction.response.send_message(
            "당신이 수정/삭제한 내역 중 되돌릴 수 있는 백업 데이터가 없습니다.",
            ephemeral=True,
        )
        return

    category_name = backup["category_name"]
    title = backup["title"]
    op_type = backup["op_type"]

    msg_type = "수정" if op_type == "edit" else "삭제"

    text = (
        f"📦 당신이 가장 최근에 {msg_type}한 내역\n"
        f"- 카테고리: `{category_name}`\n"
        f"- 제목: `{title}`\n\n"
        "해당 정보를 직전 상태로 되돌리겠습니까?"
    )

    view = RestoreBackupView(
        backup_id=backup["id"],
        guild_id=guild.id,
        category_name=category_name,
        title=title,
        requester_id=interaction.user.id,
    )

    await interaction.response.send_message(
        text,
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

# =============================
# on_ready
# =============================

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

# =============================
# 메인 실행
# =============================

if __name__ == "__main__":
    bot.run(TOKEN)
