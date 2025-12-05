import asyncio
from typing import List, Optional, Tuple

import asyncpg

from config import DATABASE_URL
from models import BackupConflict

DB_POOL: Optional[asyncpg.Pool] = None
DB_LOCK = asyncio.Lock()


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        # 카테고리
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

        # 글
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

        # 기여자
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

        # 개인 백업 테이블
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
            ADD COLUMN IF NOT EXISTS op_type TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE wiki_article_backups
            ADD COLUMN IF NOT EXISTS actor_id BIGINT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE wiki_article_backups
            ADD COLUMN IF NOT EXISTS backed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )

        # 스냅샷 백업 테이블 (데이터 정리 시점 전체 스냅샷, 최대 3일 보관)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_snapshot_backups (
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
                snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            ALTER TABLE wiki_snapshot_backups
            ADD COLUMN IF NOT EXISTS snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )

        # 유지보수 메타 테이블 (마지막 정리 시각)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_maintenance_meta (
                id INTEGER PRIMARY KEY,
                last_cleanup_at TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO wiki_maintenance_meta (id, last_cleanup_at)
            VALUES (1, NULL)
            ON CONFLICT (id) DO NOTHING;
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


async def db_backup_current_article(
    conn: asyncpg.Connection,
    article_id: int,
    op_type: str,
    actor_id: int,
):
    """
    현재 글 상태를 개인 백업 테이블에 저장.

    - 사용자별(= guild_id + actor_id 기준)로 백업을 '최근 5개'까지만 유지.
    - 어떤 유저가 6번째 백업을 생성하면 가장 오래된 1개가 밀려나서 삭제됨.
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

    guild_id = art_row["guild_id"]

    # 새 백업 추가
    await conn.execute(
        """
        INSERT INTO wiki_article_backups
            (guild_id, article_id, category_name, title, content,
             created_by_id, created_by_name, created_at, updated_at,
             op_type, actor_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """,
        guild_id,
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

    # 같은 (guild_id, actor_id)에 대해 '최근 5개'만 남기고 나머지 삭제
    await conn.execute(
        """
        DELETE FROM wiki_article_backups b
        WHERE b.guild_id = $1
          AND b.actor_id = $2
          AND b.id NOT IN (
              SELECT id
              FROM wiki_article_backups
              WHERE guild_id = $1
                AND actor_id = $2
              ORDER BY backed_at DESC
              LIMIT 5
          );
        """,
        guild_id,
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


async def db_get_backups_for_user(
    guild_id: int,
    user_id: int,
    limit: int = 5,
) -> List[asyncpg.Record]:
    """
    해당 길드 + 해당 유저 기준으로 '최근 N개' 개인 백업 목록 조회.
    단, 마지막 정리 시각(last_cleanup_at) 이후에 생성된 백업만 대상.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        last_cleanup_at = await conn.fetchval(
            "SELECT last_cleanup_at FROM wiki_maintenance_meta WHERE id=1"
        )

        if last_cleanup_at is None:
            rows = await conn.fetch(
                """
                SELECT id, article_id, category_name, title, content,
                       created_by_id, created_by_name, created_at, updated_at,
                       op_type, backed_at, actor_id
                FROM wiki_article_backups
                WHERE guild_id=$1 AND actor_id=$2
                ORDER BY backed_at DESC
                LIMIT $3
                """,
                guild_id,
                user_id,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, article_id, category_name, title, content,
                       created_by_id, created_by_name, created_at, updated_at,
                       op_type, backed_at, actor_id
                FROM wiki_article_backups
                WHERE guild_id=$1 AND actor_id=$2
                  AND backed_at > $4
                ORDER BY backed_at DESC
                LIMIT $3
                """,
                guild_id,
                user_id,
                limit,
                last_cleanup_at,
            )

        return rows


async def db_check_backup_conflict(backup_id: int) -> BackupConflict:
    """
    특정 *개인 백업*을 복구하기 전에,
    같은 정보를 다른 사용자가 이후에 수정/삭제했는지 확인.

    return: (conflict_type, other_user_id)
      - conflict_type: "none", "edited_by_other", "deleted_by_other"
      - other_user_id: 충돌을 일으킨 사용자 (없으면 None)
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        b = await conn.fetchrow(
            """
            SELECT id, guild_id, article_id, category_name, title,
                   backed_at, actor_id, op_type
            FROM wiki_article_backups
            WHERE id=$1
            """,
            backup_id,
        )
        if not b:
            return "none", None

        guild_id = b["guild_id"]
        article_id = b["article_id"]
        category_name = b["category_name"]
        title = b["title"]
        backed_at = b["backed_at"]
        actor_id = b["actor_id"]

        # 1) article_id 가 남아 있는 경우
        if article_id is not None:
            later = await conn.fetchrow(
                """
                SELECT actor_id, op_type
                FROM wiki_article_backups
                WHERE article_id=$1
                  AND backed_at > $2
                ORDER BY backed_at DESC
                LIMIT 1
                """,
                article_id,
                backed_at,
            )
            if later and later["actor_id"] and later["actor_id"] != actor_id:
                if later["op_type"] == "edit":
                    return "edited_by_other", later["actor_id"]
                elif later["op_type"] == "delete":
                    return "deleted_by_other", later["actor_id"]

            return "none", None

        # 2) article_id 가 NULL 인 경우 (이미 글 삭제된 상태)
        later_del = await conn.fetchrow(
            """
            SELECT actor_id
            FROM wiki_article_backups
            WHERE guild_id=$1
              AND category_name=$2
              AND title=$3
              AND op_type='delete'
              AND backed_at > $4
            ORDER BY backed_at DESC
            LIMIT 1
            """,
            guild_id,
            category_name,
            title,
            backed_at,
        )
        if later_del and later_del["actor_id"] and later_del["actor_id"] != actor_id:
            return "deleted_by_other", later_del["actor_id"]

        return "none", None


async def db_upsert_article(
    guild_id: int,
    category_name: str,
    title: str,
    content: str,
    user_id: int,
    user_name: str,
):
    """
    새 글 '생성 전용'.
    - 동일 카테고리+제목이 이미 있으면 "dup" 반환, DB 변경 없음
    - 성공 시 ("created", 1) 반환
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
    제목 + 내용 수정 (제목 변경 시 중복 체크 포함).
    - 수정 전에 개인 백업 저장.
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

            # 수정 전 개인 백업
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

            # 삭제 전 개인 백업
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


async def db_get_snapshots_for_article(
    guild_id: int,
    category_name: str,
    title: str,
    limit: int = 10,
) -> List[asyncpg.Record]:
    """
    스냅샷 테이블에서 특정 글(카테고리+제목)에 대한 최근 스냅샷 목록 조회
    (데이터 정리에서 3일 이상 지난 것은 이미 삭제됨)
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, article_id, guild_id, category_name, title, content,
                   created_by_id, created_by_name, created_at, updated_at, snapshot_at
            FROM wiki_snapshot_backups
            WHERE guild_id=$1 AND category_name=$2 AND title=$3
            ORDER BY snapshot_at DESC
            LIMIT $4
            """,
            guild_id,
            category_name,
            title,
            limit,
        )
        return rows


async def compact_backups_once():
    """
    백업/스냅샷 정리 (24시간마다 실행):

    1) 현재 존재하는 모든 글을 스냅샷(최신 데이터)으로 wiki_snapshot_backups 에 저장
    2) snapshot_at 기준으로 3일이 지난 스냅샷 삭제
    3) 개인 백업(wiki_article_backups)에서
       - 같은 (article_id, actor_id) 그룹 안에서 가장 최신 백업 1개만 남기고 나머지 삭제
       - article_id IS NULL (이미 삭제된 글) 백업은 모두 삭제
    4) wiki_maintenance_meta.last_cleanup_at = NOW()
       -> 이후 /wiki_backup_restore 는 이 시각 이후의 백업만 복구 가능
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1) 현재 존재하는 글 전체 스냅샷 저장
            articles = await conn.fetch(
                """
                SELECT a.id, a.guild_id, a.title, a.content,
                       a.created_by_id, a.created_by_name,
                       a.created_at, a.updated_at,
                       c.name AS category_name
                FROM wiki_articles a
                JOIN wiki_categories c ON a.category_id = c.id
                """
            )
            for row in articles:
                await conn.execute(
                    """
                    INSERT INTO wiki_snapshot_backups
                        (guild_id, article_id, category_name, title, content,
                         created_by_id, created_by_name, created_at, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                    row["guild_id"],
                    row["id"],
                    row["category_name"],
                    row["title"],
                    row["content"],
                    row["created_by_id"],
                    row["created_by_name"],
                    row["created_at"],
                    row["updated_at"],
                )

            # 2) 3일 지난 스냅샷 삭제
            delete_old_snapshots = await conn.execute(
                """
                DELETE FROM wiki_snapshot_backups
                WHERE snapshot_at < NOW() - INTERVAL '3 days';
                """
            )

            # 3-1) 살아있는 글의 과거 개인 백업 정리
            delete_non_latest = await conn.execute(
                """
                WITH latest AS (
                    SELECT MAX(id) AS id
                    FROM wiki_article_backups
                    WHERE article_id IS NOT NULL
                    GROUP BY article_id, actor_id
                )
                DELETE FROM wiki_article_backups b
                WHERE b.article_id IS NOT NULL
                  AND b.id NOT IN (SELECT id FROM latest);
                """
            )

            # 3-2) 이미 삭제된 글의 개인 백업 삭제
            delete_orphans = await conn.execute(
                """
                DELETE FROM wiki_article_backups
                WHERE article_id IS NULL;
                """
            )

            # 4) 정리 시각 기록
            await conn.execute(
                """
                INSERT INTO wiki_maintenance_meta (id, last_cleanup_at)
                VALUES (1, NOW())
                ON CONFLICT (id)
                DO UPDATE SET last_cleanup_at = EXCLUDED.last_cleanup_at;
                """
            )

    print(
        "⏱️ 백업 정리 1회 실행 완료.\n"
        f"- 개인 백업 정리 결과: {delete_non_latest}\n"
        f"- 고아(삭제된 글) 개인 백업 삭제 결과: {delete_orphans}\n"
        f"- 3일 지난 스냅샷 삭제 결과: {delete_old_snapshots}"
    )
