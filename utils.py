import re
from typing import List, Tuple
from urllib.parse import urlsplit

import asyncpg
import discord

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def split_content_and_images(content: str) -> Tuple[str, List[str]]:
    """
    내용 문자열 안에서 이미지 URL을 찾아:
    - 내용에서는 [이미지1], [이미지2] ... 로 치환
    - 실제 URL 리스트를 반환
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
    글 1개를 여러 Embed로 분리:
    - 첫 Embed: 본문 + 작성자/기여자 정보
    - 이후 Embeds: 이미지 전용
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
    디스코드 제한(메시지당 최대 10개 embed)에 맞춰 여러 번 나눠 전송
    """
    if not embeds:
        return

    max_embeds = 10
    first_chunk = embeds[:max_embeds]
    await interaction.response.send_message(embeds=first_chunk, ephemeral=ephemeral)

    remaining = embeds[max_embeds:]
    for i in range(0, len(remaining), max_embeds):
        chunk = remaining[i : i + max_embeds]
        await interaction.followup.send(embeds=chunk, ephemeral=ephemeral)
