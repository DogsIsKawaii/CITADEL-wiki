import os
from dataclasses import dataclass, field
from typing import Dict

import discord
from discord.ext import commands
from discord import app_commands

# -----------------------------
# 환경 변수에서 ID 읽기 헬퍼
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
WIKI_ADMIN_ROLE_ID = env_int("WIKI_ADMIN_ROLE_ID")    # 삭제 가능 역할 ID
WIKI_EDITOR_ROLE_ID = env_int("WIKI_EDITOR_ROLE_ID")  # 추가/수정/조회 역할 ID

# 이 길드에만 슬래시 명령어를 등록
GUILD_OBJECT = discord.Object(id=ALLOWED_GUILD_ID)

# 기본 카테고리 (예시)
CATEGORIES = ["공지", "게임", "봇사용법"]


# -----------------------------
# 봇 기본 세팅
# -----------------------------
intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# -----------------------------
# 권한 체크 함수
# -----------------------------
def is_allowed_guild(interaction: discord.Interaction) -> bool:
    """지정된 서버에서만 Slash 명령어가 동작하도록 체크"""
    return interaction.guild is not None and interaction.guild.id == ALLOWED_GUILD_ID


def has_wiki_admin_role(interaction: discord.Interaction) -> bool:
    """삭제 명령어 전용 역할 체크"""
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_ADMIN_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_role(interaction: discord.Interaction) -> bool:
    """추가/수정/조회 명령어 전용 역할 체크"""
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == WIKI_EDITOR_ROLE_ID for role in interaction.user.roles)


def has_wiki_editor_or_admin(interaction: discord.Interaction) -> bool:
    """에디터 역할이나 관리자 역할 둘 중 하나라도 있으면 통과"""
    if not isinstance(interaction.user, discord.Member):
        return False
    role_ids = {role.id for role in interaction.user.roles}
    return (WIKI_EDITOR_ROLE_ID in role_ids) or (WIKI_ADMIN_ROLE_ID in role_ids)


# -----------------------------
# 데이터 구조 (메모리용)
# -----------------------------
@dataclass
class Article:
    title: str
    content: str
    category: str
    created_by_id: int
    created_by_name: str  # 최초 작성자 이름
    contributors: Dict[int, int] = field(default_factory=dict)  # user_id -> 기여 횟수


# wiki_data[guild_id][category][title] = Article
wiki_data: Dict[int, Dict[str, Dict[str, Article]]] = {}


def get_guild_store(guild_id: int) -> Dict[str, Dict[str, Article]]:
    """길드별 위키 저장소 가져오기 (없으면 생성)"""
    return wiki_data.setdefault(guild_id, {})


# -----------------------------
# 새 글 작성용 UI (카테고리 선택 + 모달)
# -----------------------------
class NewArticleModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"[{category}] 새 위키 글 작성")
        self.category = category

        self.title_input = discord.ui.TextInput(
            label="제목",
            max_length=100
        )
        self.content_input = discord.ui.TextInput(
            label="내용",
            style=discord.TextStyle.paragraph,
            max_length=2000
        )

        self.add_item(self.title_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("길드 안에서만 사용할 수 있어요.", ephemeral=True)
            return

        store = get_guild_store(guild.id)
        category_store = store.setdefault(self.category, {})

        title = self.title_input.value.strip()
        content = self.content_input.value.strip()

        if not title or not content:
            await interaction.response.send_message(
                "제목과 내용을 모두 입력해 주세요.",
                ephemeral=True,
            )
            return

        # 새 글인지, 기존 글 덮어쓰기(실질적 수정)인지 확인
        article = category_store.get(title)

        if article is None:
            # 새 글
            article = Article(
                title=title,
                content=content,
                category=self.category,
                created_by_id=user.id,
                created_by_name=user.display_name,
            )
            # 최초 작성도 기여 1회로 카운트
            article.contributors[user.id] = 1
            category_store[title] = article
            msg = "새 글이 등록되었습니다."
        else:
            # 기존 글 수정 (여기서는 /wiki_new 로 같은 제목을 쓰면 덮어쓰기)
            article.content = content
            article.contributors[user.id] = article.contributors.get(user.id, 0) + 1
            msg = "기존 글이 수정되었습니다."

        total_contrib = article.contributors[user.id]

        await interaction.response.send_message(
            f"✅ [{self.category}] `{title}` 저장 완료!\n"
            f"작성/수정자: {user.mention} (이 글에 {total_contrib}번째 기여)",
            ephemeral=True,
        )


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=c) for c in CATEGORIES]
        super().__init__(
            placeholder="카테고리를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        await interaction.response.send_modal(NewArticleModal(category))


class NewArticleView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.add_item(CategorySelect())


# -----------------------------
# 명령어: 새 글 등록 (/wiki_new)
# -----------------------------
@bot.tree.command(
    name="wiki_new",
    description="위키에 새 글을 등록합니다.",
    guild=GUILD_OBJECT,  # 길드 전용 명령어
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)  # 에디터 또는 관리자
async def wiki_new(interaction: discord.Interaction):
    """카테고리 선택 → 모달로 제목/내용 입력"""
    view = NewArticleView()
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


# -----------------------------
# 명령어: 글 조회 (/wiki_view)
# 카테고리는 choice, 제목은 직접 입력
# -----------------------------
category_choices = [
    app_commands.Choice(name=c, value=c) for c in CATEGORIES
]


@bot.tree.command(
    name="wiki_view",
    description="위키 글을 조회합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
@app_commands.describe(category="조회할 카테고리", title="글 제목")
@app_commands.choices(category=category_choices)
async def wiki_view(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    title: str,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("길드 안에서만 사용할 수 있어요.", ephemeral=True)
        return

    store = get_guild_store(guild.id)
    category_store = store.get(category.value, {})
    article = category_store.get(title)

    if article is None:
        await interaction.response.send_message(
            f"[{category.value}] 카테고리에 `{title}` 글이 없습니다.",
            ephemeral=True,
        )
        return

    # 기여자 문자열 만들기
    contrib_lines = []
    for user_id, count in article.contributors.items():
        user_mention = f"<@{user_id}>"
        contrib_lines.append(f"- {user_mention}: {count}회")

    contrib_text = "\n".join(contrib_lines) if contrib_lines else "없음"

    embed = discord.Embed(
        title=f"[{article.category}] {article.title}",
        description=article.content,
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="최초 작성자",
        value=f"{article.created_by_name} (<@{article.created_by_id}>)",
        inline=False,
    )
    embed.add_field(
        name="기여자 / 기여 횟수",
        value=contrib_text,
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=False)


@wiki_view.error
async def wiki_view_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "이 명령어를 사용할 권한이 없거나, 이 봇은 지정된 서버에서만 사용할 수 없습니다.",
            ephemeral=True,
        )


# -----------------------------
# 명령어: 글 수정 (/wiki_edit)
# - 카테고리/제목 입력 → 해당 내용이 미리 채워진 모달
# -----------------------------
class EditArticleModal(discord.ui.Modal):
    def __init__(self, article: Article):
        super().__init__(title=f"[{article.category}] 글 수정: {article.title}")
        self.article = article

        self.content_input = discord.ui.TextInput(
            label="내용",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            default=article.content,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("길드 안에서만 사용할 수 있어요.", ephemeral=True)
            return

        # 내용 수정
        new_content = self.content_input.value.strip()
        if not new_content:
            await interaction.response.send_message("내용이 비어 있을 수는 없습니다.", ephemeral=True)
            return

        self.article.content = new_content
        # 기여 횟수 +1
        self.article.contributors[user.id] = self.article.contributors.get(user.id, 0) + 1
        total_contrib = self.article.contributors[user.id]

        await interaction.response.send_message(
            f"✏️ `{self.article.title}` 글이 수정되었습니다.\n"
            f"{user.mention} 이(가) 이 글에 {total_contrib}번째 기여를 했습니다.",
            ephemeral=True,
        )


@bot.tree.command(
    name="wiki_edit",
    description="위키 글을 수정합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_editor_or_admin)
@app_commands.describe(category="수정할 카테고리", title="글 제목")
@app_commands.choices(category=category_choices)
async def wiki_edit(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    title: str,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("길드 안에서만 사용할 수 있어요.", ephemeral=True)
        return

    store = get_guild_store(guild.id)
    category_store = store.get(category.value, {})
    article = category_store.get(title)

    if article is None:
        await interaction.response.send_message(
            f"[{category.value}] 카테고리에 `{title}` 글이 없습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(EditArticleModal(article))


@wiki_edit.error
async def wiki_edit_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "이 명령어를 사용할 권한이 없거나, 이 봇은 지정된 서버에서만 사용할 수 없습니다.",
            ephemeral=True,
        )


# -----------------------------
# 명령어: 글 삭제 (/wiki_delete)
# - 특정 역할만 가능 (관리자 역할)
# -----------------------------
@bot.tree.command(
    name="wiki_delete",
    description="위키 글을 삭제합니다.",
    guild=GUILD_OBJECT,
)
@app_commands.check(is_allowed_guild)
@app_commands.check(has_wiki_admin_role)
@app_commands.describe(category="삭제할 카테고리", title="글 제목")
@app_commands.choices(category=category_choices)
async def wiki_delete(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    title: str,
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("길드 안에서만 사용할 수 있어요.", ephemeral=True)
        return

    store = get_guild_store(guild.id)
    category_store = store.get(category.value, {})

    if title not in category_store:
        await interaction.response.send_message(
            f"[{category.value}] 카테고리에 `{title}` 글이 없습니다.",
            ephemeral=True,
        )
        return

    del category_store[title]

    await interaction.response.send_message(
        f"🗑️ [{category.value}] `{title}` 글이 삭제되었습니다.",
        ephemeral=True,
    )


@wiki_delete.error
async def wiki_delete_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # 서버 체크 실패 or 역할 체크 실패 둘 다 여기로 들어옴
        await interaction.response.send_message(
            "삭제 권한이 없거나, 이 봇이 동작하도록 허용된 서버가 아닙니다.",
            ephemeral=True,
        )


# -----------------------------
# on_ready: 명령어 싱크 + 로그
# -----------------------------
@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")
    print("✅ DB 초기화 완료 (메모리 저장소 사용)")

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
    bot.run(TOKEN)
