import re
import discord

from app.core.database import update_incident_metric
from app.core.redis_client import delete_pending_approval, get_pending_approval
from app.services.git_runner import create_github_pr, cleanup_branch


class ApprovePRButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"incident:approve:(?P<incident_id>[a-zA-Z0-9_-]+)",
):
    def __init__(self, incident_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="PR 생성 승인",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"incident:approve:{incident_id}",
            )
        )
        self.incident_id = incident_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "ApprovePRButton":
        return cls(match.group("incident_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        approval_data = await get_pending_approval(self.incident_id)
        if not approval_data:
            await interaction.followup.send(
                "⚠️ 이미 처리되었거나 만료된 승인 요청입니다.", ephemeral=True
            )
            return

        try:
            pr_url = await create_github_pr(
                incident_id=self.incident_id,
                branch_name=approval_data["branch_name"],
                error_category=approval_data["error_category"],
                patch_explanation=approval_data["patch_explanation"],
                affected_tests=approval_data["affected_tests"],
                executed_tests_count=approval_data["executed_tests_count"],
                total_tests_count=approval_data["total_tests_count"],
            )

            await update_incident_metric(
                self.incident_id,
                status="PR_CREATED",
                pr_url=pr_url,
                resolved=True,
            )
            await delete_pending_approval(self.incident_id)

            # 원본 메시지 버튼 비활성화
            disabled_view = discord.ui.View()
            disabled_view.add_item(
                discord.ui.Button(
                    label=f"PR 생성 완료 ({interaction.user.display_name})",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            if interaction.message:
                await interaction.message.edit(view=disabled_view)

            await interaction.followup.send(
                f"🚀 **{interaction.user.display_name}**님의 승인으로 GitHub PR이 생성되었습니다.\n🔗 {pr_url}"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ GitHub PR 생성 중 오류가 발생했습니다: `{e}`")


class RejectPRButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"incident:reject:(?P<incident_id>[a-zA-Z0-9_-]+)",
):
    def __init__(self, incident_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="수정 반려",
                style=discord.ButtonStyle.danger,
                emoji="✖️",
                custom_id=f"incident:reject:{incident_id}",
            )
        )
        self.incident_id = incident_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "RejectPRButton":
        return cls(match.group("incident_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        approval_data = await get_pending_approval(self.incident_id)
        if not approval_data:
            await interaction.followup.send(
                "⚠️ 이미 처리되었거나 만료된 요청입니다.", ephemeral=True
            )
            return

        await cleanup_branch(approval_data["branch_name"])
        await update_incident_metric(
            self.incident_id,
            status="REJECTED",
            resolved=True,
        )
        await delete_pending_approval(self.incident_id)

        disabled_view = discord.ui.View()
        disabled_view.add_item(
            discord.ui.Button(
                label=f"반려됨 ({interaction.user.display_name})",
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        )
        if interaction.message:
            await interaction.message.edit(view=disabled_view)

        await interaction.followup.send(
            f"🛑 **{interaction.user.display_name}**님이 패치를 반려하여 임시 브랜치를 정리했습니다."
        )


def build_approval_view(incident_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovePRButton(incident_id))
    view.add_item(RejectPRButton(incident_id))
    return view
