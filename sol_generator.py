# sol_generator.py
import os
import io
import json
import base64
import logging
import datetime
import asyncio
import shutil
from typing import Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

import aiosqlite
import aiohttp
from PIL import Image

from cryptography.fernet import Fernet

from solders.keypair import Keypair
from solders.pubkey import Pubkey

def generate_keypair_bytes() -> Tuple[bytes, str]:
    kp = Keypair()
    secret = bytes(kp)
    pub = str(kp.pubkey())
    return secret, pub

def pubkey_from_bytes(b: bytes) -> str:
    return str(Pubkey.from_bytes(b[32:]))

def _unpack_rpc_response(resp):
    if resp is None:
        return ("no_response", None)

    if isinstance(resp, dict):
        err = resp.get("error")
        if "result" in resp:
            return err, resp.get("result")
        return err, resp.get("value") or resp.get("result")


    err = getattr(resp, "error", None)
    result = getattr(resp, "result", None)
    if result is None:
        result = getattr(resp, "value", None)

    if result is None:
        try:
            d = getattr(resp, "__dict__", None)
            if isinstance(d, dict):
                err = err or d.get("error")
                result = d.get("result") or d.get("value")
        except Exception:
            pass

    return err, result

KEYPAIR_BACKEND = "solders"

try:
    from solana.rpc.async_api import AsyncClient
    from solders.pubkey import Pubkey
    from solana.rpc.types import TxOpts
except Exception:
    AsyncClient = None
    Pubkey = None

logger = logging.getLogger("sol_generator")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

# Mainnet Configuration
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.solana.com").strip()
TOKEN_2022_PROGRAM_ID = os.getenv("TOKEN_2022_PROGRAM_ID", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb").strip()
SQLITE_PATH = os.getenv("SOL_SQLITE_PATH", "data/sol_generator.sqlite3")
FERNET_KEY = os.getenv("SIM_ENCRYPTION_KEY", "")
CMD_PREFIX = os.getenv("COMMAND_PREFIX", ",")

if not RPC_URL:
    logger.warning("RPC_URL not set. Operations will be offline unless RPC is provided.")

if FERNET_KEY == "":
    logger.critical("SIM_ENCRYPTION_KEY must be provided in environment for encryption at rest.")

def get_fernet() -> Fernet:
    if not FERNET_KEY:
        raise RuntimeError("Encryption key not configured (SIM_ENCRYPTION_KEY).")
    return Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


async def ensure_db():
    """Ensure database and schema exist."""
    db_dir = os.path.dirname(SQLITE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(SQLITE_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                pubkey TEXT NOT NULL,
                encrypted_private_key BLOB NOT NULL,
                wallet_created_at TEXT NOT NULL,
                token_mint TEXT,
                token_account TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                mint_address TEXT PRIMARY KEY,
                owner_discord_id TEXT NOT NULL,
                metadata_uri TEXT,
                created_at TEXT NOT NULL,
                supply INTEGER NOT NULL,
                decimals INTEGER NOT NULL
            )
            """
        )
        await db.commit()


async def db_get_user(discord_id: str) -> Optional[dict]:
    async with aiosqlite.connect(SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return dict(row)


async def db_insert_user(discord_id: str, pubkey: str, encrypted_priv: bytes, created_at: str):
    async with aiosqlite.connect(SQLITE_PATH) as db:
        await db.execute(
            "INSERT INTO users (discord_id, pubkey, encrypted_private_key, wallet_created_at) VALUES (?, ?, ?, ?)",
            (discord_id, pubkey, encrypted_priv, created_at),
        )
        await db.commit()


async def db_delete_user(discord_id: str):
    async with aiosqlite.connect(SQLITE_PATH) as db:
        await db.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
        await db.commit()
        
def explorer_link_for_pubkey(pubkey: str) -> str:
    return f"https://solscan.io/account/{pubkey}"


def explorer_link_for_tx(tx_sig: str) -> str:
    return f"https://solscan.io/tx/{tx_sig}"


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def embed_base(title: str, description: str = "") -> discord.Embed:
    e = discord.Embed(title=title, description=description, timestamp=datetime.datetime.utcnow())
    e.set_footer(text="Mainnet — Token-2022")
    return e


async def validate_image_url(image_url: str) -> Tuple[bool, str]:
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(image_url, timeout=15) as resp:
                if resp.status != 200:
                    return False, f"Failed to fetch image: HTTP {resp.status}"
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("image/"):
                    return False, f"Invalid content-type: {ctype}"
                content = await resp.read()
                if len(content) > 100 * 1024:
                    return False, f"File size too large: {len(content)} bytes (max 100 KB)"
                try:
                    img = Image.open(io.BytesIO(content))
                    w, h = img.size
                    if w != h:
                        return False, f"Image is not square: {w}x{h}"
                    if (w, h) not in ((512, 512), (1024, 1024)):
                        return False, f"Invalid dimensions: {w}x{h} (allowed: 512×512 or 1024×1024)"
                except Exception as e:
                    return False, f"Failed to parse image: {e}"
                return True, "Image valid"
        except Exception as e:
            return False, f"Failed to fetch image: {e}"


async def get_sol_balance(pubkey: str) -> Optional[float]:
    if AsyncClient is None:
        return None
    try:
        async with AsyncClient(RPC_URL) as c:
            resp = await c.get_balance(Pubkey.from_string(pubkey))
            err, result = _unpack_rpc_response(resp)
            if err:
                logger.debug("get_balance error: %s", err)
                return None
            lamports = None
            if isinstance(result, int):
                lamports = result
            elif isinstance(result, dict):
                lamports = result.get("value")
            else:
                lamports = getattr(result, "value", None)
                if lamports is None:
                    lamports = getattr(resp, "value", None)

            if lamports is None:
                logger.debug("get_balance: unable to parse lamports from response: %s", resp)
                return None

            return lamports / 1_000_000_000.0
    except Exception as e:
        logger.error(f"Error fetching balance for {pubkey}: {e}")
        return None



async def create_wallet_for_user(discord_id: str, start_with: Optional[str] = None) -> Tuple[bool, dict]:
    if not FERNET_KEY:
        return False, {"error": "Encryption key not configured (SIM_ENCRYPTION_KEY)."}
    if KEYPAIR_BACKEND is None:
        return False, {"error": "No keypair backend available (solders or solana keypair required)."}

    secret_bytes, pubkey = generate_keypair_bytes()
    f = get_fernet()
    encrypted = f.encrypt(secret_bytes)
    created_at = now_iso()
    await db_insert_user(discord_id, pubkey, encrypted, created_at)
    return True, {"pubkey": pubkey, "created_at": created_at, "enc": base64.b64encode(encrypted).decode()}


class SolGeneratorCog(commands.Cog, name="sol_generator"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db_ready_task = None

    async def cog_load(self) -> None:
        await ensure_db()
        logger.info("SolGenerator DB ready at %s", SQLITE_PATH)

    async def _send_reply(self, ctx_or_inter, embed: discord.Embed, ephemeral: bool = True):
        if isinstance(ctx_or_inter, commands.Context):
            ctx = ctx_or_inter
            if ephemeral:
                try:
                    await ctx.author.send(embed=embed)
                    await ctx.reply("✅ I've sent you the result via DM (sensitive information).")
                except discord.Forbidden:
                    await ctx.reply(embed=embed)
            else:
                await ctx.reply(embed=embed)
        else:
            inter = ctx_or_inter
            if not inter.response.is_done():
                await inter.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await inter.followup.send(embed=embed, ephemeral=ephemeral)

    @app_commands.command(name="wallet_create", description="Create a Mainnet Solana wallet for your Discord account.")
    @app_commands.describe(start_with="Optional ASCII prefix hint for grind (advisory only/ignored in simple gen). Default: 'dad'")
    async def slash_wallet_create(self, interaction: discord.Interaction, start_with: Optional[str] = "dad"):
        await self._cmd_wallet_create(interaction, start_with)

    @commands.command(name="wallet_create", help="Create a Mainnet Solana wallet for your Discord account. Usage: wallet_create [start_with]")
    async def prefix_wallet_create(self, ctx: commands.Context, start_with: Optional[str] = "dad"):
        await self._cmd_wallet_create(ctx, start_with)

    async def _cmd_wallet_create(self, ctx_or_inter, start_with: Optional[str]):
        if not isinstance(start_with, str):
            start_with = "dad"
        if len(start_with) > 32:
            embed = embed_base("Invalid prefix", "The `start_with` prefix is too long (max 32 chars).")
            return await self._send_reply(ctx_or_inter, embed, ephemeral=True)

        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if user:
            e = embed_base("Wallet Exists", "You already have a wallet.")
            e.add_field(name="Public Key", value=user["pubkey"], inline=False)
            e.add_field(name="Actions", value="Use `/wallet_show` or `wallet_show` to view details.", inline=False)
            e.add_field(name="Solscan", value=explorer_link_for_pubkey(user["pubkey"]), inline=False)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        success, details = await create_wallet_for_user(str(discord_id), start_with=start_with)
        if not success:
            e = embed_base("Wallet creation failed", details.get("error", "Unknown"))
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        e = embed_base("Wallet created", "A new Mainnet wallet was created for your Discord account.")
        e.add_field(name="Public Key", value=details["pubkey"], inline=False)
        e.add_field(name="Created At (UTC)", value=details["created_at"], inline=False)
        e.add_field(name="Solscan", value=explorer_link_for_pubkey(details["pubkey"]), inline=False)
        e.add_field(name="Important", value="This is a MAINNET wallet. It requires real SOL to transact.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="wallet_show", description="Show your wallet details (ephemeral).")
    async def slash_wallet_show(self, interaction: discord.Interaction):
        await self._cmd_wallet_show(interaction)

    @commands.command(name="wallet_show", help="Show your wallet details. Usage: wallet_show")
    async def prefix_wallet_show(self, ctx: commands.Context):
        await self._cmd_wallet_show(ctx)

    async def _cmd_wallet_show(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "You do not have a wallet yet. Create one with `/wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        pubkey = user["pubkey"]
        e = embed_base("Wallet Info", "")
        e.add_field(name="Public Key", value=pubkey, inline=False)
        
        balance = await get_sol_balance(pubkey)
        if balance is None:
            e.add_field(name="SOL Balance", value="Unavailable (RPC error or not connected)", inline=False)
        else:
            e.add_field(name="SOL Balance", value=f"{balance:.9f} SOL", inline=False)

        if user.get("token_mint"):
            e.add_field(name="Linked Token Mint", value=user["token_mint"], inline=False)
            e.add_field(name="Token Account", value=user.get("token_account") or "Not created", inline=False)

        e.add_field(name="Solscan", value=explorer_link_for_pubkey(pubkey), inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="buy_sol", description="Guide to purchasing SOL for Mainnet.")
    async def slash_buy_sol(self, interaction: discord.Interaction):
        await self._cmd_buy_sol(interaction)

    @commands.command(name="buy_sol", help="Guide to purchasing SOL for Mainnet. Usage: buy_sol")
    async def prefix_buy_sol(self, ctx: commands.Context):
        await self._cmd_buy_sol(ctx)

    async def _cmd_buy_sol(self, ctx_or_inter):
        e = embed_base("Acquire SOL", "On Mainnet, you cannot use a free faucet. You must purchase SOL from an exchange.")
        e.add_field(name="Coinbase", value="[Buy SOL on Coinbase](https://www.coinbase.com/price/solana)", inline=False)
        e.add_field(name="Why?", value="All transactions on Solana Mainnet (account creation, minting, transfers) require a small fee paid in SOL.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="balance", description="Show SOL balance and linked token balances (ephemeral by default).")
    async def slash_balance(self, interaction: discord.Interaction):
        await self._cmd_balance(interaction)

    @commands.command(name="balance", help="Show SOL and token balances. Usage: balance")
    async def prefix_balance(self, ctx: commands.Context):
        await self._cmd_balance(ctx)

    async def _cmd_balance(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Create one with `/wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        pubkey = user["pubkey"]
        e = embed_base("Balance", "")
        balance = await get_sol_balance(pubkey)
        if balance is None:
            e.add_field(name="SOL Balance", value="Unavailable (RPC error).", inline=False)
        else:
            e.add_field(name="SOL Balance", value=f"{balance:.9f} SOL", inline=False)

        e.add_field(name="Solscan", value=explorer_link_for_pubkey(pubkey), inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="liquidity", description="Guide to adding liquidity on Raydium.")
    async def slash_liquidity(self, interaction: discord.Interaction):
        await self._cmd_liquidity(interaction)

    @commands.command(name="liquidity", help="Guide to adding liquidity on Raydium. Usage: liquidity")
    async def prefix_liquidity(self, ctx: commands.Context):
        await self._cmd_liquidity(ctx)

    async def _cmd_liquidity(self, ctx_or_inter):
        e = embed_base("Liquidity Pools", "To make your token purchasable, you can create a Liquidity Pool (LP) on a DEX like Raydium.")
        e.add_field(name="Raydium", value="[Create Pool on Raydium](https://raydium.io/liquidity/create/)", inline=False)
        e.add_field(name="Info", value="You will need to deposit both your custom token and a base token (usually SOL) to start the pool.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="monitor", description="How to monitor transactions and wallets.")
    async def slash_monitor(self, interaction: discord.Interaction):
        await self._cmd_monitor(interaction)

    @commands.command(name="monitor", help="How to monitor transactions. Usage: monitor")
    async def prefix_monitor(self, ctx: commands.Context):
        await self._cmd_monitor(ctx)

    async def _cmd_monitor(self, ctx_or_inter):
        e = embed_base("Transaction Monitoring", "Use Solscan to view real-time data for any wallet or transaction.")
        e.add_field(name="Solscan", value="https://solscan.io/", inline=False)
        e.add_field(name="Tip", value="Paste your wallet address or token mint address in the search bar to see all activity.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="burn", description="Guide to burning tokens.")
    async def slash_burn(self, interaction: discord.Interaction):
        await self._cmd_burn(interaction)

    @commands.command(name="burn", help="Guide to burning tokens. Usage: burn")
    async def prefix_burn(self, ctx: commands.Context):
        await self._cmd_burn(ctx)

    async def _cmd_burn(self, ctx_or_inter):
        e = embed_base("Burning Tokens", "Burning removes tokens from circulation permanently.")
        e.add_field(name="Sol Incinerator", value="[Sol Incinerator](https://sol-incinerator.com/)", inline=False)
        e.add_field(name="Manual", value="You can also send tokens to a dead address, or use `spl-token burn` if you have the balance.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="security", description="Guide to disabling mint/freeze authority.")
    async def slash_security(self, interaction: discord.Interaction):
        await self._cmd_security(interaction)

    @commands.command(name="security", help="Guide to disabling mint/freeze authority. Usage: security")
    async def prefix_security(self, ctx: commands.Context):
        await self._cmd_security(ctx)

    async def _cmd_security(self, ctx_or_inter):
        e = embed_base("Token Security", "To gain trust, it is recommended to revoke Mint and Freeze authorities so no new tokens can be created or frozen.")
        e.add_field(name="Disable Mint Authority", value="`spl-token authorize <YOUR_TOKEN_MINT_ADDRESS> mint --disable`", inline=False)
        e.add_field(name="Disable Freeze Authority", value="`spl-token authorize <YOUR_TOKEN_MINT_ADDRESS> freeze --disable`", inline=False)
        e.add_field(name="Note", value="Replace `<YOUR_TOKEN_MINT_ADDRESS>` with your actual mint address. These actions are irreversible!", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="initialize_metadata", description="Initialize Token-2022 metadata.")
    @app_commands.describe(image_url="URL to image (512×512 or 1024×1024, <100KB)", name="Token name", symbol="Token symbol (short)", uri="Optional metadata URI")
    async def slash_initialize_metadata(self, interaction: discord.Interaction, image_url: str, name: str, symbol: str, uri: Optional[str] = None):
        await self._cmd_initialize_metadata(interaction, image_url, name, symbol, uri)

    @commands.command(name="initialize_metadata", help="Initialize Token metadata. Usage: initialize_metadata <image_url> <name> <symbol> [uri]")
    async def prefix_initialize_metadata(self, ctx: commands.Context, image_url: str, name: str, symbol: str, uri: Optional[str] = None):
        await self._cmd_initialize_metadata(ctx, image_url, name, symbol, uri)

    async def _cmd_initialize_metadata(self, ctx_or_inter, image_url: str, name: str, symbol: str, uri: Optional[str]):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "You do not have a wallet yet. Create one with `/wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        mint = user.get("token_mint")
        if not mint:
            e = embed_base("No mint linked", "You must link a mint to your account first.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if shutil.which("spl-token") is None:
            e = embed_base("Missing dependency", "`spl-token` CLI not found in PATH.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if not uri:
            meta = {"name": name, "symbol": symbol, "description": "Token-2022 metadata", "image": image_url}
            b64 = base64.b64encode(json.dumps(meta).encode()).decode()
            uri_to_use = f"data:application/json;base64,{b64}"
        else:
            uri_to_use = uri

        cmd = ["spl-token", "initialize-metadata", mint, name, symbol, uri_to_use]
        if RPC_URL:
             cmd = ["spl-token", "--url", RPC_URL, "initialize-metadata", mint, name, symbol, uri_to_use]
        if TOKEN_2022_PROGRAM_ID:
            cmd.insert(2, "--program-id")
            cmd.insert(3, TOKEN_2022_PROGRAM_ID)

        try:

            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            stdout = out.decode(errors="ignore").strip()
            stderr = err.decode(errors="ignore").strip()
        except Exception as e:
            e = embed_base("Execution error", str(e))
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if proc.returncode != 0:
            e = embed_base("Metadata initialization failed", stderr or stdout)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        e = embed_base("Metadata initialized", "Metadata initialization transaction submitted.")
        e.add_field(name="Mint", value=mint, inline=False)
        e.add_field(name="Result", value=stdout[:1000], inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="mint_token", description="(OWNER) Create a Token-2022 mint (Mainnet).")
    async def slash_mint_token(self, interaction: discord.Interaction):
        await self._cmd_mint_token(interaction)

    @commands.command(name="mint_token", help="Create a Token-2022 mint (Mainnet). Usage: mint_token")
    async def prefix_mint_token(self, ctx: commands.Context):
        await self._cmd_mint_token(ctx)

    async def _cmd_mint_token(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))

        
        e = embed_base("Create Token Mint", "To create a token on Mainnet, you need SOL for rent exemption.")
        e.add_field(name="Command", value=f"`spl-token create-token --program-id {TOKEN_2022_PROGRAM_ID} --decimals 9`", inline=False)
        e.add_field(name="Note", value="Run this in your terminal. After creation, use `/link_mint <address>` (if implemented) or manually update DB.", inline=False)
        e.add_field(name="Auto-Setup", value="The bot currently doesn't autosign Mainnet transactions for safety. Please allow the developer to integrate a signing mechanism if desired.", inline=False)

        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="wallet_delete", description="Delete your wallet from the DB")
    async def slash_wallet_delete(self, interaction: discord.Interaction):
        await self._cmd_wallet_delete(interaction)

    @commands.command(name="wallet_delete", help="Delete your wallet. Usage: wallet_delete")
    async def prefix_wallet_delete(self, ctx: commands.Context):
        await self._cmd_wallet_delete(ctx)

    async def _cmd_wallet_delete(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Nothing to delete.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        await db_delete_user(str(discord_id))
        e = embed_base("Wallet deleted", "Your wallet record has been removed.")
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)


async def setup(bot: commands.Bot):
    await ensure_db()
    await bot.add_cog(SolGeneratorCog(bot))