# Discord: cheesuskrist | Roblox: RoboKnight1133
# simulator.py
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

# Encryption
from cryptography.fernet import Fernet

from solders.keypair import Keypair

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

logger = logging.getLogger("simulator")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

RPC_URL = os.getenv("SIM_RPC_URL", "").strip()
TOKEN_2022_PROGRAM_ID = os.getenv("SIM_TOKEN2022_PROGRAM_ID", "").strip()
SQLITE_PATH = os.getenv("SIM_SQLITE_PATH", "simulator.sqlite3")
FERNET_KEY = os.getenv("SIM_ENCRYPTION_KEY", "")
CMD_PREFIX = os.getenv("COMMAND_PREFIX", ",")

if not RPC_URL:
    logger.warning("SIM_RPC_URL not set. Operations will be offline unless RPC is provided.")

if FERNET_KEY == "":
    logger.critical("SIM_ENCRYPTION_KEY must be provided in environment for encryption at rest.")

def get_fernet() -> Fernet:
    if not FERNET_KEY:
        raise RuntimeError("Encryption key not configured (SIM_ENCRYPTION_KEY).")
    return Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


async def ensure_db():
    """
    Ensure the simulator database and schema exist.
    This database mimics the mainnet database but acts strictly as a sandbox for Devnet tokens,
    simulated balances, and synthetic mints.
    """
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


def is_devnet_rpc(url: str) -> bool:
    url = url.lower()
    return "devnet" in url or "localhost" in url or "127.0.0.1" in url


def explorer_link_for_pubkey(pubkey: str, endpoint: str = RPC_URL) -> str:
    return f"https://explorer.solana.com/address/{pubkey}?cluster=devnet"


def explorer_link_for_tx(tx_sig: str) -> str:
    return f"https://explorer.solana.com/tx/{tx_sig}?cluster=devnet&showTransaction=true&ephemeral=true"


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def embed_base(title: str, description: str = "") -> discord.Embed:
    e = discord.Embed(title=title, description=description, timestamp=datetime.datetime.utcnow())
    e.set_footer(text="Devnet only — Token-2022")
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



async def request_airdrop_rpc(pubkey: str, amount_sol: float = 1.0) -> Tuple[bool, str]:
    """
    Interfaces with the Solana Devnet RPC to request airdrops of fake SOL for testing.
    Safety mechanism ensures this cannot run on mainnet endpoints.
    """
    if AsyncClient is None:
        return False, "RPC client not available in environment."
    if not is_devnet_rpc(RPC_URL):
        return False, "RPC URL is not devnet — airdrop disabled (devnet-only policy)."
    lamports = int(amount_sol * 1_000_000_000)
    async with AsyncClient(RPC_URL) as c:
        resp = await c.request_airdrop(Pubkey.from_string(pubkey), lamports)

        err, result = _unpack_rpc_response(resp)
        if err:
            try:
                return False, json.dumps(err)
            except Exception:
                return False, str(err)

        txsig = None
        if isinstance(result, str):
            txsig = result
        elif isinstance(result, dict):
            txsig = result.get("value") or result.get("signature") or result.get("tx")
        else:
            txsig = getattr(result, "value", None) or getattr(result, "signature", None) or getattr(resp, "result", None)

        if txsig is None:
            try:
                return False, json.dumps(result)
            except Exception:
                return False, str(result)

        return True, txsig


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


class SimulatorCog(commands.Cog, name="simulator"):
    """
    Cog responsible for Devnet and simulated interactions.
    It provides a safe, sandbox environment for users to test creating wallets,
    claiming airdrops, minting synthetic tokens locally, and understanding the mechanics
    before they commit to real Mainnet transactions.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db_ready_task = None

    async def cog_load(self) -> None:
        await ensure_db()
        logger.info("Simulator DB ready.")

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

    @app_commands.command(name=f"sim-wallet_create", description="Create a devnet-only Solana wallet for your Discord account.")
    @app_commands.describe(start_with="Optional ASCII prefix hint for grind (advisory only). Default: 'dad'")
    async def slash_wallet_create(self, interaction: discord.Interaction, start_with: Optional[str] = "dad"):
        await self._cmd_wallet_create(interaction, start_with)

    @commands.command(name="sim-wallet_create", help="(sim) Create a devnet-only Solana wallet for your Discord account. Usage: sim-wallet_create [start_with]")
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
            e.add_field(name="Actions", value="Use `/sim-wallet_show` or `sim-wallet_show` to view details. Use `/sim-wallet_delete` to remove (only allowed after token cleanup).", inline=False)
            e.add_field(name="Explorer (devnet)", value=explorer_link_for_pubkey(user["pubkey"]), inline=False)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        success, details = await create_wallet_for_user(str(discord_id), start_with=start_with)
        if not success:
            e = embed_base("Wallet creation failed", details.get("error", "Unknown"))
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        e = embed_base("Wallet created", "A new devnet wallet was created for your Discord account.")
        e.add_field(name="Public Key", value=details["pubkey"], inline=False)
        e.add_field(name="Created At (UTC)", value=details["created_at"], inline=False)
        e.add_field(name="Explorer (devnet)", value=explorer_link_for_pubkey(details["pubkey"]), inline=False)
        e.set_footer(text="This is devnet only — do not use mainnet addresses here.")
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-wallet_show", description="Show your wallet details (ephemeral).")
    async def slash_wallet_show(self, interaction: discord.Interaction):
        await self._cmd_wallet_show(interaction)

    @commands.command(name="sim-wallet_show", help="(sim) Show your wallet details. Usage: sim-wallet_show")
    async def prefix_wallet_show(self, ctx: commands.Context):
        await self._cmd_wallet_show(ctx)

    async def _cmd_wallet_show(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "You do not have a wallet yet. Create one with `/sim-wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        pubkey = user["pubkey"]
        e = embed_base("Wallet Info", "")
        e.add_field(name="Public Key", value=pubkey, inline=False)
        e.add_field(name="RPC URL", value=f"{RPC_URL or 'Not configured in environment'}", inline=False)

        balance = await get_sol_balance(pubkey)
        if balance is None:
            e.add_field(name="SOL Balance", value="Unavailable (RPC client not installed or RPC error)", inline=False)
        else:
            e.add_field(name="SOL Balance", value=f"{balance:.9f} SOL", inline=False)

        if user.get("token_mint"):
            e.add_field(name="Linked Token Mint", value=user["token_mint"], inline=False)
            e.add_field(name="Token Account", value=user.get("token_account") or "Not created", inline=False)

        e.add_field(name="Explorer (devnet)", value=explorer_link_for_pubkey(pubkey), inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-air_drop", description="Guide to claim devnet SOL from faucet (or attempt RPC airdrop if available).")
    @app_commands.describe(amount="Amount of SOL to request (devnet). Default 1.0")
    async def slash_airdrop(self, interaction: discord.Interaction, amount: Optional[float] = 1.0):
        await self._cmd_airdrop(interaction, amount)

    @commands.command(name="sim-air_drop", help="(sim) Guide to claim devnet SOL from faucet or attempt RPC airdrop. Usage: sim-air_drop [amount]")
    async def prefix_airdrop(self, ctx: commands.Context, amount: Optional[float] = 1.0):
        await self._cmd_airdrop(ctx, amount)

    async def _cmd_airdrop(self, ctx_or_inter, amount: float = 1.0):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Create a wallet first with `/sim-wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        pubkey = user["pubkey"]
        faucet_url = f"https://faucet.solana.com/?address={pubkey}"
        e = embed_base("Airdrop Guidance", f"To claim Devnet SOL, use the official faucet or let me attempt an RPC airdrop (if RPC allows).")
        e.add_field(name="Faucet (web)", value=faucet_url, inline=False)

        rpc_attempt_msg = "RPC airdrop not attempted."
        if AsyncClient is not None and RPC_URL and is_devnet_rpc(RPC_URL):
            success, info = await request_airdrop_rpc(pubkey, amount)
            if success:
                txsig = info
                rpc_attempt_msg = f"RPC airdrop requested. Tx: {explorer_link_for_tx(txsig)}"
            else:
                rpc_attempt_msg = f"RPC airdrop failed: {info}"
        else:
            rpc_attempt_msg = "RPC airdrop not available (client not installed or RPC not configured to devnet)."

        e.add_field(name="RPC Airdrop", value=rpc_attempt_msg, inline=False)
        e.add_field(name="Next Steps", value="After claiming, run `/sim-balance` to confirm.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-balance", description="Show SOL balance and linked token balances (ephemeral by default).")
    async def slash_balance(self, interaction: discord.Interaction):
        await self._cmd_balance(interaction)

    @commands.command(name="sim-balance", help="(sim) Show SOL and token balances. Usage: sim-balance")
    async def prefix_balance(self, ctx: commands.Context):
        await self._cmd_balance(ctx)

    async def _cmd_balance(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Create one with `/sim-wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        pubkey = user["pubkey"]
        e = embed_base("Balance", "")
        balance = await get_sol_balance(pubkey)
        if balance is None:
            e.add_field(name="SOL Balance", value="Unavailable (RPC not configured or error).", inline=False)
        else:
            e.add_field(name="SOL Balance", value=f"{balance:.9f} SOL", inline=False)

        if user.get("token_mint"):
            mint = user["token_mint"]
            token_acc = user.get("token_account") or "Not created"
            e.add_field(name="Linked Token Mint", value=mint, inline=False)
            e.add_field(name="Token Account", value=token_acc, inline=False)
            e.add_field(name="Token Balances", value="(Token balances require Token-2022 RPC calls; run token-specific commands)", inline=False)

        e.add_field(name="Explorer (devnet)", value=explorer_link_for_pubkey(pubkey), inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-initialize_metadata", description="Initialize Token-2022 metadata with strict image requirements (512 or 1024 square, <100KB).")
    @app_commands.describe(image_url="URL to image (512×512 or 1024×1024, <100KB)", name="Token name", symbol="Token symbol (short)", uri="Optional metadata URI")
    async def slash_initialize_metadata(self, interaction: discord.Interaction, image_url: str, name: str, symbol: str, uri: Optional[str] = None):
        await self._cmd_initialize_metadata(interaction, image_url, name, symbol, uri)

    @commands.command(name="sim-initialize_metadata", help="(sim) Initialize Token metadata. Usage: sim-initialize_metadata <image_url> <name> <symbol> [uri]")
    async def prefix_initialize_metadata(self, ctx: commands.Context, image_url: str, name: str, symbol: str, uri: Optional[str] = None):
        await self._cmd_initialize_metadata(ctx, image_url, name, symbol, uri)

    async def _cmd_initialize_metadata(self, ctx_or_inter, image_url: str, name: str, symbol: str, uri: Optional[str]):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "You do not have a wallet yet. Create one with `/sim-wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        mint = user.get("token_mint")
        if not mint or mint.startswith("SIM_MINT_"):
            e = embed_base("No on-chain mint linked", "Your account does not appear to have a real on-chain mint linked. The simulator's `SIM_MINT_*` entries are local-only. Create a real mint (e.g. via `spl-token create-token` or your preferred SDK) and store that mint address in `users.token_mint` before running this command.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if not is_devnet_rpc(RPC_URL):
            e = embed_base("Devnet required", "RPC is not configured to devnet. Metadata initialization is blocked (devnet-only policy).")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if shutil.which("spl-token") is None:
            e = embed_base("Missing dependency", "`spl-token` CLI not found in PATH. Install @solana/spl-token and ensure it's on PATH for on-chain metadata initialization.")
            e.add_field(name="Install hint", value="https://docs.solana.com/cli/install-solana-cli-tools", inline=False)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if not uri:
            meta = {"name": name, "symbol": symbol, "description": "Token-2022 metadata (created via bot)", "image": image_url}
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
        except FileNotFoundError as e:
            e = embed_base("Execution failed", f"Failed to run spl-token CLI: {e}")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)
        except Exception as e:
            e = embed_base("Execution error", str(e))
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if proc.returncode != 0:
            e = embed_base("On-chain metadata initialization failed", stderr or stdout or "Unknown error from spl-token CLI")
            e.add_field(name="CLI stdout", value=(stdout[:1000] + "..." if len(stdout) > 1000 else stdout) or "(empty)", inline=False)
            e.add_field(name="CLI stderr", value=(stderr[:1000] + "..." if len(stderr) > 1000 else stderr) or "(empty)", inline=False)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        txsig = None
        for line in (stdout + "\n" + stderr).splitlines():
            if "Signature" in line or "transaction" in line.lower():
                parts = line.split()
                for p in parts:
                    if len(p) >= 20 and len(p) <= 96:
                        txsig = p
                        break
            if txsig:
                break

        e = embed_base("Metadata initialized on-chain", "Metadata initialization transaction submitted (devnet).")
        e.add_field(name="Mint", value=mint, inline=False)
        e.add_field(name="Name / Symbol", value=f"{name} / {symbol}", inline=True)
        e.add_field(name="Metadata URI", value=(uri_to_use if len(uri_to_use) < 1000 else (uri_to_use[:997] + "...")), inline=False)
        if txsig:
            e.add_field(name="Explorer (tx)", value=explorer_link_for_tx(txsig), inline=False)
        else:
            e.add_field(name="CLI output (inspect)", value=(stdout[:1000] + "..." if len(stdout) > 1000 else stdout) or (stderr[:1000] + "..." if len(stderr) > 1000 else stderr), inline=False)

        e.set_footer(text="Devnet on-chain metadata initialization via spl-token CLI. Ensure the mint authority keypair is available to sign the transaction.")
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-wallet_delete", description="Delete your wallet from the simulator DB")
    async def slash_wallet_delete(self, interaction: discord.Interaction):
        await self._cmd_wallet_delete(interaction)

    @commands.command(name="sim-wallet_delete", help="(sim) Delete your wallet (only allowed if token supply = 0 and no token accounts). Usage: sim-wallet_delete")
    async def prefix_wallet_delete(self, ctx: commands.Context):
        await self._cmd_wallet_delete(ctx)

    async def _cmd_wallet_delete(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Nothing to delete.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if user.get("token_mint"):
            async with aiosqlite.connect(SQLITE_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM tokens WHERE mint_address = ?", (user["token_mint"],))
                token = await cur.fetchone()
                if token and token["supply"] > 0:
                    e = embed_base("Cannot delete wallet", "Token supply is non-zero. Burn circulating tokens first or run `/sim-burn_or_delete_token`.")
                    e.add_field(name="Mint", value=user["token_mint"], inline=False)
                    e.add_field(name="Current Supply", value=str(token["supply"]), inline=False)
                    return await self._send_reply(ctx_or_inter, e, ephemeral=True)

                if user.get("token_account"):
                    e = embed_base("Cannot delete wallet", "Token account exists. Close all token accounts and ensure supply is zero before deletion.")
                    e.add_field(name="Token Account", value=user["token_account"], inline=False)
                    return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        await db_delete_user(str(discord_id))
        e = embed_base("Wallet deleted", "Your wallet record and encrypted key have been removed from the simulator DB.")
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-mint_token", description="(OWNER) Create a Token-2022 mint for your wallet (devnet only).")
    @app_commands.describe(start_with="Optional mnemonic prefix (advisory). Default 'mnt'")
    async def slash_mint_token(self, interaction: discord.Interaction, start_with: Optional[str] = "mnt"):
        await self._cmd_mint_token(interaction, start_with)

    @commands.command(name="sim-mint_token", help="(sim) Create a Token-2022 mint (devnet only). Usage: sim-mint_token [start_with]")
    async def prefix_mint_token(self, ctx: commands.Context, start_with: Optional[str] = "mnt"):
        await self._cmd_mint_token(ctx, start_with)

    async def _cmd_mint_token(self, ctx_or_inter, start_with: Optional[str] = "mnt"):

        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user:
            e = embed_base("No wallet found", "Create your wallet first with `/sim-wallet_create`.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if not is_devnet_rpc(RPC_URL):
            e = embed_base("Devnet required", "RPC is not configured to devnet. Minting is blocked (devnet-only policy).")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        estimated_sol_required = 0.002
        sol_balance = await get_sol_balance(user["pubkey"])
        if sol_balance is None or sol_balance < estimated_sol_required:
            e = embed_base("Insufficient SOL", f"Estimated required for mint creation: {estimated_sol_required:.9f} SOL; available: {sol_balance if sol_balance is not None else 'Unknown'} SOL.")
            e.add_field(name="Faucet", value=f"https://faucet.solana.com/?address={user['pubkey']}", inline=False)
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        synthetic_mint = f"SIM_MINT_{str(discord_id)}_{int(datetime.datetime.utcnow().timestamp())}"
        created_at = now_iso()
        async with aiosqlite.connect(SQLITE_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO tokens (mint_address, owner_discord_id, metadata_uri, created_at, supply, decimals) VALUES (?, ?, ?, ?, ?, ?)",
                (synthetic_mint, str(discord_id), None, created_at, 0, 9),
            )
            await db.execute("UPDATE users SET token_mint = ? WHERE discord_id = ?", (synthetic_mint, str(discord_id)))
            await db.commit()

        e = embed_base("Token mint recorded (local simulation)", "A local Token-2022 mint entry was created for demonstration. To perform the real on-chain mint, run the SDK calls from a secure operator environment; see CLI example below.")
        e.add_field(name="Simulated Mint Address", value=synthetic_mint, inline=False)
        e.add_field(name="Decimals", value="9 (fixed)", inline=True)
        e.add_field(name="CLI Example", value=f"spl-token create-token --program-id {TOKEN_2022_PROGRAM_ID or '<TOKEN_2022_PROGRAM_ID>'} --decimals 9", inline=False)
        e.add_field(name="Next Steps", value="Use `/sim-create_token_account` then `/sim-mint` to simulate minting supply.", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-create_token_account", description="Create the owner's token account for the minted token (simulation).")
    async def slash_create_token_account(self, interaction: discord.Interaction):
        await self._cmd_create_token_account(interaction)

    @commands.command(name="sim-create_token_account", help="(sim) Create owner's token account for minted token (simulation). Usage: sim-create_token_account")
    async def prefix_create_token_account(self, ctx: commands.Context):
        await self._cmd_create_token_account(ctx)

    async def _cmd_create_token_account(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user or not user.get("token_mint"):
            e = embed_base("No linked mint", "You must have a minted token linked (use `/sim-mint_token`).")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        synthetic_token_account = f"SIM_ACC_{user['pubkey'][:8]}_{int(datetime.datetime.utcnow().timestamp())}"
        async with aiosqlite.connect(SQLITE_PATH) as db:
            await db.execute("UPDATE users SET token_account = ? WHERE discord_id = ?", (synthetic_token_account, str(discord_id)))
            await db.commit()

        e = embed_base("Token account created (local simulation)", "An associated token account record has been created in the simulator DB.")
        e.add_field(name="Token Account", value=synthetic_token_account, inline=False)
        e.add_field(name="Mint", value=user["token_mint"], inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-mint", description="Mint tokens to owner's token account (simulation).")
    @app_commands.describe(amount="Amount of tokens to mint (base units)")
    async def slash_mint(self, interaction: discord.Interaction, amount: int):
        await self._cmd_mint(interaction, amount)

    @commands.command(name="sim-mint", help="(sim) Mint tokens to owner's account (simulation). Usage: sim-mint <amount>")
    async def prefix_mint(self, ctx: commands.Context, amount: int):
        await self._cmd_mint(ctx, amount)

    async def _cmd_mint(self, ctx_or_inter, amount: int):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user or not user.get("token_mint") or not user.get("token_account"):
            e = embed_base("Preconditions not met", "You must have a linked mint and token account (use `/sim-mint_token` and `/sim-create_token_account`).")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        async with aiosqlite.connect(SQLITE_PATH) as db:
            cur = await db.execute("SELECT supply FROM tokens WHERE mint_address = ?", (user["token_mint"],))
            row = await cur.fetchone()
            current_supply = row[0] if row else 0
            new_supply = current_supply + amount
            await db.execute("UPDATE tokens SET supply = ? WHERE mint_address = ?", (new_supply, user["token_mint"]))
            await db.commit()

        e = embed_base("Mint simulated", f"Minted {amount} units to your token account (simulation).")
        e.add_field(name="New Supply", value=str(new_supply), inline=False)
        e.add_field(name="Mint", value=user["token_mint"], inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-transfer", description="Transfer tokens to another pubkey (simulation supports fund-recipient behavior).")
    @app_commands.describe(to_pubkey="Recipient public key", amount="Amount to send (base units)")
    async def slash_transfer(self, interaction: discord.Interaction, to_pubkey: str, amount: int):
        await self._cmd_transfer(interaction, to_pubkey, amount)

    @commands.command(name="sim-transfer", help="(sim) Transfer tokens to another pubkey (simulation). Usage: sim-transfer <to_pubkey> <amount>")
    async def prefix_transfer(self, ctx: commands.Context, to_pubkey: str, amount: int):
        await self._cmd_transfer(ctx, to_pubkey, amount)

    async def _cmd_transfer(self, ctx_or_inter, to_pubkey: str, amount: int):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user or not user.get("token_mint") or not user.get("token_account"):
            e = embed_base("Preconditions not met", "You must have a linked mint and token account.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        if not isinstance(to_pubkey, str) or len(to_pubkey) < 8:
            e = embed_base("Invalid recipient pubkey", "Provided recipient pubkey looks invalid.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        async with aiosqlite.connect(SQLITE_PATH) as db:
            cur = await db.execute("SELECT supply FROM tokens WHERE mint_address = ?", (user["token_mint"],))
            row = await cur.fetchone()
            current_supply = row[0] if row else 0

            if current_supply < amount:
                e = embed_base("Insufficient token balance", f"Token supply ({current_supply}) is less than requested transfer ({amount}) in this simulation mode.")
                return await self._send_reply(ctx_or_inter, e, ephemeral=True)

            new_supply = current_supply
            await db.commit()

        e = embed_base("Transfer simulated", "Transfer completed in simulation (no on-chain effect).")
        e.add_field(name="From", value=user["token_account"], inline=True)
        e.add_field(name="To (recipient)", value=to_pubkey, inline=True)
        e.add_field(name="Amount", value=str(amount), inline=False)
        e.add_field(name="Note", value="If recipient had no token account, in a real flow an associated token account would be created and funded (fee estimate displayed before action).", inline=False)
        return await self._send_reply(ctx_or_inter, e, ephemeral=True)

    @app_commands.command(name="sim-burn_or_delete_token", description="Burn all circulating tokens and delete token record (simulation).")
    async def slash_burn_or_delete_token(self, interaction: discord.Interaction):
        await self._cmd_burn_or_delete_token(interaction)

    @commands.command(name="sim-burn_or_delete_token", help="(sim) Burn circulating tokens and allow token deletion (simulation). Usage: sim-burn_or_delete_token")
    async def prefix_burn_or_delete_token(self, ctx: commands.Context):
        await self._cmd_burn_or_delete_token(ctx)

    async def _cmd_burn_or_delete_token(self, ctx_or_inter):
        discord_id = (ctx_or_inter.user.id if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author.id)
        user = await db_get_user(str(discord_id))
        if not user or not user.get("token_mint"):
            e = embed_base("No token found", "You have no linked token mint to burn or delete.")
            return await self._send_reply(ctx_or_inter, e, ephemeral=True)

        async with aiosqlite.connect(SQLITE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tokens WHERE mint_address = ?", (user["token_mint"],))
            token = await cur.fetchone()
            if not token:
                e = embed_base("Token record missing", "Token record not found in DB.")
                return await self._send_reply(ctx_or_inter, e, ephemeral=True)

            supply = token["supply"]
            if supply > 0:
                await db.execute("UPDATE tokens SET supply = ? WHERE mint_address = ?", (0, user["token_mint"]))
                await db.commit()
                e = embed_base("Tokens burned (simulation)", f"All {supply} units were burned in simulation. You may now delete the token record.")
                e.add_field(name="Mint", value=user["token_mint"], inline=False)
                return await self._send_reply(ctx_or_inter, e, ephemeral=True)
            else:
                await db.execute("DELETE FROM tokens WHERE mint_address = ?", (user["token_mint"],))
                await db.execute("UPDATE users SET token_mint = NULL, token_account = NULL WHERE discord_id = ?", (str(discord_id),))
                await db.commit()
                e = embed_base("Token record deleted", "Token record removed and user's token linkage cleared.")
                return await self._send_reply(ctx_or_inter, e, ephemeral=True)


async def setup(bot: commands.Bot):
    await ensure_db()
    await bot.add_cog(SimulatorCog(bot))
