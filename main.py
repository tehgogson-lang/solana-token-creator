# Discord: cheesuskrist | Roblox: RoboKnight1133
import os
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None

_cmd_prefix_env = os.getenv("COMMAND_PREFIX", "").strip()
if not _cmd_prefix_env:
    COMMAND_PREFIX = ","
elif "," in _cmd_prefix_env:
    COMMAND_PREFIX = [p.strip() for p in _cmd_prefix_env.split(",") if p.strip()]
else:
    COMMAND_PREFIX = _cmd_prefix_env

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MESSAGE_CONTENT_INTENT = os.getenv("MESSAGE_CONTENT_INTENT", "true").lower() in ("1", "true", "yes")

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("premium_bot")

intents = discord.Intents.default()
intents.message_content = MESSAGE_CONTENT_INTENT
intents.guilds = True
intents.members = True

class PremiumBot(commands.Bot):
    """
    Main bot class extending commands.Bot. 
    Handles dynamic loading of simulator and generator extensions (cogs) 
    and handles global slash command syncing.
    """
    def __init__(self):
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None
        )

    async def setup_hook(self) -> None:
        await self.load_extension("simulator")
        await self.load_extension("sol_generator")

    async def on_ready(self):
        """
        Triggered when the bot has successfully established a connection with Discord.
        We set the bot's rich presence and synchronize slash commands globally or to a specific guild.
        """
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{COMMAND_PREFIX}help or /help"
        )

        await self.change_presence(activity=activity)
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        try:
            synced = await self.tree.sync()
            logger.info(f"✅ Synced {len(synced)} commands globally")

            if GUILD_ID:
                guild_obj = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild_obj)
                synced_guild = await self.tree.sync(guild=guild_obj)
                logger.info(f"✅ Synced {len(synced_guild)} commands to guild {GUILD_ID}")

        except Exception as e:
            logger.error("❌ Failed to sync commands", exc_info=e)

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: Exception
    ) -> None:
        """
        Global error handler for slash commands.
        Gracefully intercepts permission failures and cooldowns to send user-friendly error embeds,
        preventing raw tracebacks from clogging the console.
        """

        if isinstance(error, app_commands.MissingPermissions):
            missing = [perm.replace('_', ' ').title() for perm in error.missing_permissions]
            embed = discord.Embed(
                title="Access Denied",
                description=f"You need:\n`{', '.join(missing)}`",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if isinstance(error, app_commands.BotMissingPermissions):
            missing = [perm.replace('_', ' ').title() for perm in error.missing_permissions]
            embed = discord.Embed(
                title="Bot Missing Permissions",
                description=f"I need:\n`{', '.join(missing)}`",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if isinstance(error, app_commands.CheckFailure):
            embed = discord.Embed(
                description=str(error),
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if isinstance(error, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                description=f"Please wait **{error.retry_after:.1f}s**.",
                color=discord.Color.orange()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        logger.error(
            "Unhandled application command error: %s",
            interaction.command,
            exc_info=error
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                "An unexpected error occurred.",
                ephemeral=True
            )
        else:
            try:
                await interaction.followup.send(
                    "An unexpected error occurred.",
                    ephemeral=True
                )
            except Exception:
                pass

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            embed = discord.Embed(
                title="Missing Argument",
                description=f"The parameter `{error.param.name}` is required.\n\n**Usage:** `{usage}`",
                color=discord.Color.orange()
            )
            await ctx.reply(embed=embed)
            return

        if isinstance(error, commands.BadArgument):
            await ctx.reply(f"❌ **Invalid Input:** Please check your arguments and try again.")
            return

        if isinstance(error, commands.MissingPermissions):
            missing = [perm.replace('_', ' ').title() for perm in error.missing_permissions]
            await ctx.reply(
                f"🚫 You are missing permissions: `{', '.join(missing)}`"
            )
            return

        if isinstance(error, commands.CheckFailure):
            await ctx.reply(f"⚠️ {str(error)}")
            return

        logger.error(
            "Unhandled prefix command error: %s",
            ctx.command,
            exc_info=error
        )

bot = PremiumBot()

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
