import logging
from typing import Any, Optional
import discord
from discord.ext import commands

from app.core.config import get_settings
from app.bot.discord_views import ApprovePRButton, RejectPRButton, build_approval_view

logger = logging.getLogger(__name__)


class IncidentDiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # 컨테이너 재시작 후에도 기존 메시지 버튼 이벤트를 수신할 수 있도록 등록
        self.add_dynamic_items(ApprovePRButton, RejectPRButton)

    async def on_ready(self) -> None:
        logger.info("Discord Bot logged in as %s (ID: %s)", self.user, self.user.id if self.user else "N/A")


_bot_instance: Optional[IncidentDiscordBot] = None


def get_discord_bot() -> IncidentDiscordBot:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = IncidentDiscordBot()
    return _bot_instance


async def create_incident_thread(
    incident_id: str,
    service_name: str,
    environment: str,
    exception_class: str,
    endpoint: str,
    error_hash: str,
) -> Optional[str]:
    """장애 알림 채널에 기준 메시지를 올리고 전용 추적 스레드를 생성합니다."""
    settings = get_settings()
    bot = get_discord_bot()

    if not settings.DISCORD_BOT_TOKEN or not settings.DISCORD_ALERT_CHANNEL_ID or not bot.is_ready():
        logger.warning("Discord bot is not ready or channel ID is missing; skipping thread creation.")
        return None

    channel = bot.get_channel(settings.DISCORD_ALERT_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(settings.DISCORD_ALERT_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        logger.error("Configured channel %s is not a TextChannel.", settings.DISCORD_ALERT_CHANNEL_ID)
        return None

    short_ex = exception_class.split(".")[-1]
    embed = discord.Embed(
        title=f"🤖 AI 자동 복구 파이프라인 시작 (`{incident_id[:8]}`)",
        description=f"**대상 서비스:** `{service_name}` ({environment})\n**요청 경로:** `{endpoint}`\n**예외 유형:** `{short_ex}`",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Error Hash: {error_hash}")

    msg = await channel.send(embed=embed)
    thread = await msg.create_thread(
        name=f"[{incident_id[:8]}] {short_ex} 분석 및 회귀 검증",
        auto_archive_duration=1440,
    )
    return str(thread.id)


async def post_thread_update(thread_id: Optional[str], message: str) -> None:
    """스레드에 단계별 진행 로그를 남깁니다."""
    if not thread_id:
        return
    bot = get_discord_bot()
    if not bot.is_ready():
        return

    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
        if isinstance(thread, discord.Thread):
            await thread.send(message[:1950])
    except Exception as e:
        logger.warning("Failed to post update to Discord thread %s: %s", thread_id, e)


async def post_approval_card(thread_id: Optional[str], approval_data: dict[str, Any]) -> None:
    """회귀 테스트 통과 후 수정 Diff 요약과 [PR 생성 승인] 버튼을 게시합니다."""
    if not thread_id:
        return
    bot = get_discord_bot()
    if not bot.is_ready():
        return

    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
        if not isinstance(thread, discord.Thread):
            return

        incident_id = approval_data["incident_id"]
        executed = approval_data["executed_tests_count"]
        total = approval_data["total_tests_count"]
        tests_str = "\n".join(f"• `{t}`" for t in approval_data["affected_tests"]) or "• 기본 빌드 검증 통과"
        diff_preview = approval_data["patch_diff"][:950]

        embed = discord.Embed(
            title="✅ 선별 회귀 테스트 통과 — PR 생성 승인 대기",
            description=approval_data["patch_explanation"][:1000],
            color=discord.Color.green(),
        )
        embed.add_field(
            name=f"선별 실행 테스트 ({executed} / {total}개)",
            value=tests_str[:1000],
            inline=False,
        )
        embed.add_field(
            name="생성된 코드 패치 (Unified Diff)",
            value=f"```diff\n{diff_preview}\n```",
            inline=False,
        )

        view = build_approval_view(incident_id)
        await thread.send(embed=embed, view=view)
    except Exception as e:
        logger.error("Failed to post approval card to thread %s: %s", thread_id, e)
