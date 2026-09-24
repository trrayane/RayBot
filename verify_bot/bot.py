import csv
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

from students import load_students

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
VERIFIED_PATH = BASE_DIR / "verified.json"
VERIFY_LOG_PATH = BASE_DIR / "verify_log.csv"
REMINDERS_PATH = BASE_DIR / "reminders.json"
MESSAGE_STATS_PATH = BASE_DIR / "message_stats.json"
WARNS_PATH = BASE_DIR / "warns.json"
WELCOME_BG_DEFAULT = BASE_DIR / "welcome_bg.png"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sii-verify")

ETAT_LABELS = {
    "ADM": "Admis",
    "TRFU": "Transferé",
    "AJR": "Ajourné",
    "RNTG": "Non admissible",
    "RS": "Redoublant",
}

# ---- anti-spam / timeout settings ----
MAX_STRIKES = 3
STRIKE_WINDOW_SECONDS = 60
TIMEOUT_SECONDS = 5 * 60

# ---- reminders ----
MAX_REMINDERS_PER_USER = 20
REMINDER_MAX_DAYS_AHEAD = 365

# Commands that should NOT be echoed into the log channel (pure lookups, no action taken)
NO_LOG_COMMANDS = {"Voir informations"}

# ---- anti-invite-link protection ----
DISCORD_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[a-zA-Z0-9-]+",
    re.IGNORECASE,
)


# ---------------- helpers ----------------

def _get_channel_id(config: dict, key: str):
    value = config.get(key)
    if not value:
        return None
    try:
        cid = int(value)
        return cid if cid > 0 else None
    except (TypeError, ValueError):
        return None


def _is_admin(bot, user) -> bool:
    admin_ids = bot.config.get("admin_user_ids", [])
    if user.id in admin_ids:
        return True
    if isinstance(user, discord.Member):
        admin_role = bot.config.get("admin_role")
        if admin_role and discord.utils.get(user.roles, name=admin_role):
            return True
    return False


def _is_verified_member(bot, user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    role_name = bot.config.get("verified_role")
    return bool(role_name and discord.utils.get(user.roles, name=role_name))


def load_verified() -> dict:
    if VERIFIED_PATH.exists():
        try:
            with open(VERIFIED_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Could not read verified.json: %s", exc)
    return {}


def save_verified(data: dict):
    try:
        with open(VERIFIED_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning("Could not write verified.json: %s", exc)


def load_config_file() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_reminders() -> list:
    if REMINDERS_PATH.exists():
        try:
            with open(REMINDERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            log.warning("Could not read reminders.json: %s", exc)
    return []


def save_reminders(data: list):
    try:
        with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning("Could not write reminders.json: %s", exc)


def load_message_stats() -> dict:
    """Per-user message stats the bot has tracked since it started keeping them.
    Structure: {user_id_str: {"count": int, "last_content": str, "last_channel": str, "last_at": iso}}"""
    if MESSAGE_STATS_PATH.exists():
        try:
            with open(MESSAGE_STATS_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log.warning("Could not read message_stats.json: %s", exc)
    return {}


def save_message_stats(data: dict):
    try:
        with open(MESSAGE_STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning("Could not write message_stats.json: %s", exc)


def load_warns() -> dict:
    """{user_id_str: [ {"id": str, "reason": str, "by": str, "by_id": str, "at": iso}, ... ]}"""
    if WARNS_PATH.exists():
        try:
            with open(WARNS_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log.warning("Could not read warns.json: %s", exc)
    return {}


def save_warns(data: dict):
    try:
        with open(WARNS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning("Could not write warns.json: %s", exc)


def log_verification(user_id: int, username: str, display_name: str, matricule: str,
                     full_name: str = "", section: str = "", td: str = "", tp: str = "",
                     status: str = "OK"):
    file_exists = VERIFY_LOG_PATH.exists()
    try:
        with open(VERIFY_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp_utc", "discord_id", "discord_username",
                    "discord_display_name", "matricule", "student_full_name",
                    "section", "groupe_td", "groupe_tp", "status",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                user_id, username, display_name, matricule, full_name,
                section, td, tp, status,
            ])
    except Exception as exc:
        log.warning("Could not write verify_log.csv: %s", exc)


def _all_td_groups(students: dict) -> list:
    td = set()
    for s in students.values():
        if s.is_sii and s.groupe_td:
            td.add(s.groupe_td)
    return sorted(td, key=lambda x: (len(x), x))


def _parse_reminder_date(text: str):
    """
    Accept:
      - ISO 8601: 2026-09-25T14:30 or 2026-09-25 14:30
      - DD/MM/YYYY HH:MM
      - YYYY-MM-DD HH:MM
    Returns a timezone-aware UTC datetime, or None if invalid.
    """
    text = text.strip()
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]
    # Assume server runs in UTC (adjust if you prefer another TZ)
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                dt = dt.replace(hour=9, minute=0)  # default 9 AM UTC
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def _apply_role(interaction: discord.Interaction | None = None, role_name: str = "",
                      *, member: discord.Member | None = None,
                      guild: discord.Guild | None = None,
                      bot=None) -> bool:
    member = member or (interaction.user if isinstance(interaction.user, discord.Member) else None)
    guild = guild or (interaction.guild if interaction is not None else None)
    if member is None or guild is None or not role_name:
        return False
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        log.warning("Role '%s' not found on guild '%s'", role_name, guild.name)
        if bot is not None:
            await _send_log(
                bot,
                f"❌ **Rôle introuvable** — aucun rôle nommé exactement `{role_name}` sur le serveur "
                f"(vérifie l'orthographe/majuscules dans `config.json`). Concerné : {member.mention}",
                guild,
            )
        return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="MIV verification")
        return True
    except discord.Forbidden:
        log.warning("No permission to assign role '%s' to %s", role_name, member)
        if bot is not None:
            me = guild.me
            bot_top = me.top_role.name if me else "?"
            await _send_log(
                bot,
                f"❌ **Permission refusée** en assignant `{role_name}` à {member.mention}. "
                f"Vérifie que le bot a la permission **Gérer les rôles** et que son rôle "
                f"(actuellement le plus haut : `{bot_top}`) est **placé au-dessus** de `{role_name}` "
                "dans les paramètres du serveur → Rôles.",
                guild,
            )
        return False


async def _set_td_group_role(bot, member: discord.Member, td_group):
    group_role_ids = bot.config.get("group_role_ids", {})
    target_role = None
    try:
        gid = int(str(td_group))
        str_td = str(gid)
        if str_td in group_role_ids or gid in group_role_ids:
            rid = group_role_ids.get(str_td) or group_role_ids.get(gid)
            target_role = member.guild.get_role(int(rid))
    except (TypeError, ValueError):
        target_role = None

    fmt = bot.config.get("group_role_format", "G{group}")
    target_name = fmt.format(group=td_group)

    if target_role is None:
        target_role = discord.utils.get(member.guild.roles, name=target_name)
    else:
        target_name = target_role.name

    if target_role is None:
        log.warning("Target group role for TD '%s' does not exist on this server", td_group)
        await _send_log(
            bot,
            f"❌ **Rôle manquant** — `{target_name}` n'existe pas sur le serveur. "
            f"Étudiant concerné : {member.mention} (TD {td_group}). "
            "Crée le rôle avec `/creer_roles_groupes`.",
            member.guild,
        )
        return None

    group_prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
    id_vals = list(group_role_ids.values()) if isinstance(group_role_ids, dict) else []
    to_remove = []
    for r in member.roles:
        if r == target_role:
            continue
        by_name = r.name.startswith(group_prefix) and r.name[len(group_prefix):].isdigit()
        by_id = str(r.id) in [str(v) for v in id_vals]
        if by_name or by_id:
            to_remove.append(r)

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="MIV group sync")
        already_had_it = target_role in member.roles
        if not already_had_it:
            await member.add_roles(target_role, reason="MIV group assignment")
        if to_remove or not already_had_it:
            stale_note = f" · anciens rôles retirés : {', '.join(r.name for r in to_remove)}" if to_remove else ""
            await _send_log(
                bot,
                f"🎯 **Rôle de groupe attribué** — {member.mention} → **{target_name}**{stale_note}",
                member.guild,
            )
        return target_role.name
    except discord.Forbidden:
        log.warning("No permission to modify roles for %s", member)
        await _send_log(bot, f"❌ **Permissions insuffisantes** pour assigner `{target_name}` à {member.mention}", member.guild)
        return None
    except discord.HTTPException as exc:
        log.warning("HTTP error updating roles for %s: %s", member, exc)
        await _send_log(bot, f"❌ **Erreur HTTP** en assignant `{target_name}` à {member.mention} : {exc}", member.guild)
        return None


async def _assign_visitor_roles(bot, member: discord.Member, reason: str = "MIV verification failed"):
    """Assign the Visitor role and strip MIV + all group roles (failed verification)."""
    unverified_name = bot.config.get("unverified_role", "MIV Unverified")
    await _apply_role(None, unverified_name, member=member, guild=member.guild, bot=bot)

    verified_name = bot.config.get("verified_role", "MIV Verified")
    verified = discord.utils.get(member.guild.roles, name=verified_name)
    to_remove = []
    if verified and verified in member.roles:
        to_remove.append(verified)

    group_role_ids = bot.config.get("group_role_ids", {})
    id_vals = [str(v) for v in (group_role_ids.values() if isinstance(group_role_ids, dict) else [])]
    fmt = bot.config.get("group_role_format", "G{group}")
    group_prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
    for r in member.roles:
        if r == verified or r.name == unverified_name:
            continue
        by_name = r.name.startswith(group_prefix) and r.name[len(group_prefix):].isdigit()
        by_id = str(r.id) in id_vals
        if by_name or by_id:
            to_remove.append(r)

    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason=reason)
        except discord.Forbidden:
            log.warning("No permission to remove roles from %s", member)
        except discord.HTTPException as exc:
            log.warning("HTTP error removing roles from %s: %s", member, exc)


async def _notify_mods(bot, interaction: discord.Interaction | None = None,
                       matricule: str = "", student=None,
                       reason: str = "", success: bool = False,
                       *, guild: discord.Guild | None = None, user=None):
    channel_id = _get_channel_id(bot.config, "mod_channel_id")
    if channel_id is None:
        return
    guild = guild or (interaction.guild if interaction is not None else None)
    if guild is None:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        log.warning("Mod channel %s not found", channel_id)
        return
    if user is not None:
        who = f"{user} (`{user.id}`)"
    elif interaction is not None:
        who = f"{interaction.user} (`{interaction.user.id}`)"
    else:
        who = "unknown"
    name = f"{student.full_name}" if student else "unknown"
    icon = "✅" if success else "🚨"
    label = "Successful verification" if success else "Failed verification"
    msg = f"{icon} **{label}** by {who}\nMatricule: `{matricule}` · Matched: {name}"
    if reason:
        msg += f" · Reason: **{reason}**"
    try:
        await channel.send(msg)
    except discord.HTTPException as exc:
        log.warning("Failed to send mod notification: %s", exc)


# ---------------- DM verification + logging helpers ----------------

def _get_guild(bot) -> discord.Guild | None:
    gid = bot.config.get("sync_guild_id")
    if not gid:
        return bot.guilds[0] if bot.guilds else None
    try:
        return bot.get_guild(int(gid))
    except (TypeError, ValueError):
        return None


async def _send_dm(user, content, view=None) -> bool:
    try:
        await user.send(content, view=view)
        return True
    except discord.Forbidden:
        log.warning("Cannot DM %s — DMs are closed", user)
        return False
    except discord.HTTPException as exc:
        log.warning("DM to %s failed: %s", user, exc)
        return False


async def _send_log(bot, text: str, guild: discord.Guild | None = None,
                    embed: discord.Embed | None = None):
    channel_id = _get_channel_id(bot.config, "log_channel_id")
    if channel_id is None:
        return
    guild = guild or _get_guild(bot)
    if guild is None:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        log.warning("Log channel %s not found", channel_id)
        return
    try:
        if embed is not None:
            await channel.send(content=text, embed=embed)
        else:
            await channel.send(text)
    except discord.HTTPException as exc:
        log.warning("Failed to send log: %s", exc)


async def _send_message_log(bot, text: str, guild: discord.Guild | None = None,
                            embed: discord.Embed | None = None):
    """Sends message edit/delete notices to their own dedicated channel
    (message_log_channel_id), separate from the general mod log channel."""
    channel_id = _get_channel_id(bot.config, "message_log_channel_id")
    if channel_id is None:
        return
    guild = guild or _get_guild(bot)
    if guild is None:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        log.warning("Message log channel %s not found", channel_id)
        return
    try:
        if embed is not None:
            await channel.send(content=text, embed=embed)
        else:
            await channel.send(text)
    except discord.HTTPException as exc:
        log.warning("Failed to send message log: %s", exc)


def _find_a_font(size: int) -> ImageFont.FreeTypeFont:
    """Try a few common font paths (Windows/Linux), fall back to PIL's default bitmap font."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _make_gradient_background(width: int, height: int) -> Image.Image:
    """Generates a simple dark blue->purple gradient as a fallback background
    when no custom welcome_bg image has been uploaded yet."""
    img = Image.new("RGB", (width, height), "#1e1f29")
    top = (35, 39, 90)
    bottom = (88, 40, 130)
    for y in range(height):
        t = y / height
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        ImageDraw.Draw(img).line([(0, y), (width, y)], fill=(r, g, b))
    return img


async def _generate_welcome_image(bot, member: discord.Member) -> io.BytesIO:
    """Builds a welcome banner: background image (or gradient fallback) + the
    member's circular avatar + their name + the server's new member count."""
    width, height = 1000, 400

    bg_filename = bot.config.get("welcome_background", "welcome_bg.png")
    bg_path = BASE_DIR / bg_filename
    if bg_path.exists():
        bg = Image.open(bg_path).convert("RGB")
        bg = ImageOps.fit(bg, (width, height), Image.LANCZOS)
        # Darken a bit so white text stays readable over any photo.
        overlay = Image.new("RGB", (width, height), (0, 0, 0))
        bg = Image.blend(bg, overlay, 0.35)
    else:
        bg = _make_gradient_background(width, height)

    # Avatar: fetch, resize, make circular, add a white ring.
    avatar_bytes = await member.display_avatar.replace(size=256).read()
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    avatar_size = 180
    avatar = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
    avatar.putalpha(mask)

    canvas = bg.convert("RGBA")
    ax = (width - avatar_size) // 2
    ay = 45
    ring_pad = 6
    ring = Image.new("L", (avatar_size + ring_pad * 2, avatar_size + ring_pad * 2), 0)
    ImageDraw.Draw(ring).ellipse((0, 0, ring.size[0], ring.size[1]), fill=255)
    ring_img = Image.new("RGBA", ring.size, (255, 255, 255, 255))
    ring_img.putalpha(ring)
    canvas.alpha_composite(ring_img, (ax - ring_pad, ay - ring_pad))
    canvas.alpha_composite(avatar, (ax, ay))

    draw = ImageDraw.Draw(canvas)
    title_font = _find_a_font(46)
    name_font = _find_a_font(32)
    sub_font = _find_a_font(22)

    title_text = "BIENVENUE"
    tb = draw.textbbox((0, 0), title_text, font=title_font)
    draw.text(((width - (tb[2] - tb[0])) / 2, ay + avatar_size + 20), title_text,
              font=title_font, fill="white")

    name_text = member.display_name
    nb = draw.textbbox((0, 0), name_text, font=name_font)
    draw.text(((width - (nb[2] - nb[0])) / 2, ay + avatar_size + 80), name_text,
              font=name_font, fill=(230, 230, 255))

    sub_text = f"Membre #{member.guild.member_count} · {member.guild.name}"
    sb = draw.textbbox((0, 0), sub_text, font=sub_font)
    draw.text(((width - (sb[2] - sb[0])) / 2, ay + avatar_size + 125), sub_text,
              font=sub_font, fill=(200, 200, 220))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def _dedupe_block(bot, user_id: int, mat: str) -> str | None:
    owner = bot._find_user_for_matricule(mat)
    if owner is not None and owner != str(user_id):
        return f"déjà lié à un autre compte (<@{owner}>)"
    return None


async def _finalize_verification(bot, member: discord.Member, mat: str, student):
    config = bot.config
    verified_role = config.get("verified_role", "MIV Verified")
    unverified_role = config.get("unverified_role", "MIV Unverified")

    role_ok = await _apply_role(None, verified_role, member=member, guild=member.guild, bot=bot)

    unverified = discord.utils.get(member.guild.roles, name=unverified_role)
    if unverified and unverified in member.roles:
        try:
            await member.remove_roles(unverified, reason="MIV verification passed")
        except discord.Forbidden:
            log.warning("Could not remove unverified role from %s", member)

    group_role_assigned = None
    if student.groupe_td:
        group_role_assigned = await _set_td_group_role(bot, member, student.groupe_td)

    if bot.verified.get(str(member.id)) != mat:
        old_mat = bot.verified.get(str(member.id))
        if old_mat:
            bot.matricule_to_user.pop(old_mat, None)
        bot.verified[str(member.id)] = mat
        bot.matricule_to_user[mat] = str(member.id)
        save_verified(bot.verified)

    log_verification(
        member.id, str(member), getattr(member, "display_name", str(member)), mat,
        full_name=student.full_name, section=student.section,
        td=student.groupe_td, tp=student.groupe_tp, status="OK",
    )
    return role_ok, group_role_assigned


class VerifyConfirmView(discord.ui.View):
    """Buttons sent by DM: confirm that the posted matricule really is theirs."""

    def __init__(self, bot, user_id: int, mat: str, student):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.mat = mat
        self.student = student

    async def _resolve_member(self) -> discord.Member | None:
        guild = _get_guild(self.bot)
        if guild is None:
            return None
        try:
            return guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    def _lock(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="✅ C'est bien moi", style=discord.ButtonStyle.success)
    async def confirm_cb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ces boutons ne sont pas pour toi.", ephemeral=True)
            return

        member = await self._resolve_member()
        if member is None:
            self._lock()
            await interaction.response.edit_message(
                content="❌ Ton compte n'est plus membre du serveur. Contacte un admin.", view=self)
            return

        block = _dedupe_block(self.bot, self.user_id, self.mat)
        if block:
            self._lock()
            await interaction.response.edit_message(
                content=f"❌ Ce matricule est **{block}**. Vérification annulée.", view=self)
            await _send_log(self.bot, f"🚨 **Doublon bloqué en DM** — <@{self.user_id}> sur `{self.mat}` ({block})", member.guild)
            await _notify_mods(self.bot, None, self.mat, self.student,
                               f"doublon bloqué à la confirmation ({block})",
                               guild=member.guild, user=member)
            return

        role_ok, group = await _finalize_verification(self.bot, member, self.mat, self.student)

        td = self.student.groupe_td or "?"
        tp = self.student.groupe_tp or "?"
        role_note = "" if role_ok else "\n⚠️ *Rôle vérifié non attribué automatiquement — un admin a été prévenu.*"
        group_note = ""
        if group:
            group_note = f"\n🎯 Rôle de groupe attribué : **{group}**"
        elif self.student.groupe_td:
            fmt = self.bot.config.get("group_role_format", "G{group}")
            group_note = (
                f"\n⚠️ *Ton rôle de groupe `{fmt.format(group=self.student.groupe_td)}` n'existe pas encore "
                "sur le serveur — un admin a été prévenu automatiquement et va le créer, tu le recevras ensuite.*"
            )

        self._lock()
        await interaction.response.edit_message(
            content=f"✅ **Vérifié !**\n"
                    f"**{self.student.full_name}** — {self.student.palier} {self.student.specialite} "
                    f"(Section {self.student.section})\n"
                    f"Groupe TD : **{td}** · Groupe TP : **{tp}**{role_note}{group_note}\n\n"
                    f"Utilise `/mes_infos`, `/mes_groupes` ou `/mes_camarades` pour en savoir plus.",
            view=self)
        await _send_log(self.bot, f"✅ **Vérification DM réussie** — {member.mention} (`{member.id}`) lié au matricule `{self.mat}` ({self.student.full_name})", member.guild)
        await _notify_mods(self.bot, None, self.mat, self.student, "", success=True, guild=member.guild, user=member)

    @discord.ui.button(label="❌ Non, ce n'est pas moi", style=discord.ButtonStyle.danger)
    async def deny_cb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ces boutons ne sont pas pour toi.", ephemeral=True)
            return
        self._lock()
        await interaction.response.edit_message(
            content="❌ Vérification annulée. Ton message a été supprimé du salon matricule.", view=self)
        guild = _get_guild(self.bot)
        await _send_log(self.bot, f"❌ **Vérification refusée** — <@{self.user_id}> (`{self.user_id}`) a refusé le matricule `{self.mat}` ({self.student.full_name})", guild)


async def _handle_matricule_message(bot, author: discord.Member, mat: str):
    guild = author.guild

    if bot.verified.get(str(author.id)):
        await _send_dm(author, "ℹ️ Tu es **déjà vérifié.** Contacte un admin si tu veux changer de matricule.")
        await _send_log(bot, f"ℹ️ **Déjà vérifié** — {author.mention} a renvoyé un matricule alors qu'il est déjà vérifié.", guild)
        return

    block = _dedupe_block(bot, author.id, mat)
    if block:
        await _send_dm(author, f"❌ Le matricule `{mat}` est **{block}**. Vérification annulée.")
        await _send_log(bot, f"🚨 **Doublon** — {author.mention} (`{author.id}`) a tenté `{mat}` ({block}).", guild)
        await _notify_mods(bot, None, mat, None, f"doublon détecté ({block})", guild=guild, user=author)
        return

    student = bot.students.get(mat)

    if student is None:
        await _assign_visitor_roles(bot, author, reason="matricule not in list")
        await _send_dm(author, f"❌ Le matricule `{mat}` ne correspond à **aucun étudiant de la liste officielle**.\nSi tu penses que c'est une erreur, contacte un admin.")
        log_verification(author.id, str(author), getattr(author, "display_name", str(author)), mat, status="NOT_IN_LIST")
        await _send_log(bot, f"🚨 **Matricule inconnu** — {author.mention} (`{author.id}`) a soumis `{mat}` (introuvable).", guild)
        await _notify_mods(bot, None, mat, None, "non trouvé dans la liste", guild=guild, user=author)
        return

    if not student.is_sii:
        await _assign_visitor_roles(bot, author, reason="wrong speciality")
        await _send_dm(author, f"❌ Le matricule `{mat}` correspond à **{student.full_name}** mais la spécialité est **{student.specialite}**, pas MIV Student.")
        log_verification(author.id, str(author), getattr(author, "display_name", str(author)), mat,
                         full_name=student.full_name, section=student.section,
                         td=student.groupe_td, tp=student.groupe_tp, status="WRONG_SPECIALITY")
        await _send_log(bot, f"🚨 **Mauvaise spécialité** — {author.mention} a soumis `{mat}` → {student.full_name} ({student.specialite}).", guild)
        await _notify_mods(bot, None, mat, student, "mauvaise spécialité", guild=guild, user=author)
        return

    if not student.is_admis:
        await _assign_visitor_roles(bot, author, reason="not admis")
        etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
        await _send_dm(author, f"❌ Le matricule `{mat}` correspond à **{student.full_name}** mais le statut est **{etat_label}** (non admis).")
        log_verification(author.id, str(author), getattr(author, "display_name", str(author)), mat,
                         full_name=student.full_name, section=student.section,
                         td=student.groupe_td, tp=student.groupe_tp, status="NOT_ADMIS")
        await _send_log(bot, f"🚨 **Non admis** — {author.mention} a soumis `{mat}` → {student.full_name} ({student.etat}).", guild)
        await _notify_mods(bot, None, mat, student, f"statut={student.etat}", guild=guild, user=author)
        return

    sent = await _send_dm(
        author,
        f"🎓 **Confirmation d'identité**\n"
        f"Le matricule **`{mat}`** correspond à :\n"
        f"**{student.full_name}** — {student.palier} {student.specialite} (Section {student.section})\n"
        f"Groupe TD : `{student.groupe_td or '?'}` · Groupe TP : `{student.groupe_tp or '?'}`\n\n"
        f"Confirme que c'est bien toi pour être vérifié, sinon ton message sera supprimé.",
        view=VerifyConfirmView(bot, author.id, mat, student),
    )
    if not sent:
        await _send_log(bot, f"⚠️ **DM inaccessible** — {author.mention} a soumis `{mat}` mais ses DM sont fermés.", guild)
        await _notify_mods(bot, None, mat, student, "DM fermés — vérification impossible", guild=guild, user=author)
        return
    await _send_log(bot, f"📨 **Matricule reçu** — {author.mention} (`{author.id}`) a soumis `{mat}` → {student.full_name}. DM de confirmation envoyé.", guild)


# ---------------- bot ----------------

class VerifyBot(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True  # needed for member.status / member.activity in "Voir informations"
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.students: dict = {}
        self.skipped_rows = 0
        self.verified: dict = load_verified()
        self.matricule_to_user: dict = {
            mat: uid for uid, mat in self.verified.items()
        }
        self.spam_state: dict = {}
        self.reminders: list = load_reminders()
        self.message_stats: dict = load_message_stats()
        self.warns: dict = load_warns()
        self.reload_students()
        self._register_commands()
        self.tree.on_error = self.on_tree_error

    # ---------- lifecycle ----------

    async def setup_hook(self):
        self.reminder_loop.start()

        guild_id = self.config.get("sync_guild_id")
        if not guild_id:
            log.warning("sync_guild_id not set — commands will sync globally (slow).")
            try:
                synced = await self.tree.sync()
                log.info("Globally synced %d command(s): %s", len(synced), [c.name for c in synced])
            except discord.HTTPException as exc:
                log.error("Global command sync failed: %s", exc)
            return

        guild_obj = discord.Object(id=int(guild_id))
        self.tree.copy_global_to(guild=guild_obj)
        try:
            synced = await self.tree.sync(guild=guild_obj)
            if not synced:
                log.error(
                    "Synced 0 commands to guild %s. Bot likely lacks 'applications.commands' scope.",
                    guild_id,
                )
            else:
                log.info(
                    "Synced %d command(s) to guild %s: %s",
                    len(synced), guild_id, [c.name for c in synced],
                )
        except discord.Forbidden as exc:
            log.error("Forbidden when syncing to guild %s: %s", guild_id, exc)
        except discord.HTTPException as exc:
            log.error("HTTP error when syncing to guild %s: %s", guild_id, exc)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("Connected to %d guild(s): %s",
                 len(self.guilds),
                 [(g.name, g.id) for g in self.guilds])
        await self._send_startup_message()

    # ---------- reminder background task ----------

    @tasks.loop(seconds=30)
    async def reminder_loop(self):
        """Check for due reminders and DM users."""
        if not self.reminders:
            return
        now = datetime.now(timezone.utc)
        due = []
        keep = []
        for r in self.reminders:
            try:
                when = datetime.fromisoformat(r["when"])
            except Exception:
                continue
            if when <= now:
                due.append(r)
            else:
                keep.append(r)

        if not due:
            return

        # Mark reminders as consumed and persist BEFORE sending the DMs.
        # This way, even if the bot crashes/restarts mid-send, a fired
        # reminder can never be re-sent on the next startup.
        self.reminders = keep
        save_reminders(self.reminders)

        log.info("Firing %d reminder(s)", len(due))
        for r in due:
            try:
                user = self.get_user(int(r["user_id"])) or await self.fetch_user(int(r["user_id"]))
                if user is None:
                    log.warning("Reminder: user %s not found", r["user_id"])
                    continue
                embed = discord.Embed(
                    title="⏰ Reminder",
                    description=r["message"],
                    color=discord.Color.blurple(),
                    timestamp=now,
                )
                embed.set_footer(text=f"Scheduled for {r['when']} UTC")
                await user.send(embed=embed)
                await _send_log(
                    self,
                    f"⏰ **Rappel envoyé** à <@{r['user_id']}> — `{r['message'][:80]}`",
                )
            except discord.Forbidden:
                log.warning("Reminder: cannot DM user %s", r.get("user_id"))
            except discord.HTTPException as exc:
                log.warning("Reminder send failed: %s", exc)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.wait_until_ready()

    # ---------- message / interaction logging ----------

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            log.info("[DM] <%s>: %s", message.author, message.content)
            return

        self._track_message(message)

        # ---- anti-invite-link protection (whole server, admins exempt) ----
        if not _is_admin(self, message.author):
            invite_match = DISCORD_INVITE_RE.search(message.content or "")
            if invite_match:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                try:
                    warn_msg = await message.channel.send(
                        f"{message.author.mention} les liens d'invitation Discord ne sont pas autorisés ici."
                    )
                    await warn_msg.delete(delay=6)
                except discord.HTTPException:
                    pass
                embed = discord.Embed(
                    title="🔗 Lien d'invitation supprimé",
                    description=f"Lien détecté : `{invite_match.group(0)}`",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
                embed.add_field(name="Salon", value=f"#{message.channel.name}", inline=False)
                embed.set_footer(text=f"ID utilisateur : {message.author.id}")
                await _send_message_log(self, "", message.guild, embed=embed)
                return

        verify_channel_id = _get_channel_id(self.config, "verify_channel_id")
        if verify_channel_id and message.channel.id == verify_channel_id:
            now = time.monotonic()
            state = self.spam_state.setdefault(
                message.author.id,
                {"strikes": [], "until": 0.0},
            )

            if state["until"] > now:
                log.info("Muted user %s posted in verify channel — deleting", message.author)
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            content = message.content.strip()

            if content.isdigit():
                mat = content.replace(" ", "")
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                if isinstance(message.author, discord.Member):
                    await _handle_matricule_message(self, message.author, mat)
                return

            if not content.isdigit():
                state["strikes"] = [
                    t for t in state["strikes"] if now - t < STRIKE_WINDOW_SECONDS
                ]
                state["strikes"].append(now)
                count = len(state["strikes"])
                log.info("Strike %d/%d for %s in verify channel", count, MAX_STRIKES, message.author)

                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

                if count >= MAX_STRIKES:
                    state["until"] = now + TIMEOUT_SECONDS
                    state["strikes"] = []

                    if isinstance(message.author, discord.Member):
                        try:
                            until = datetime.now(timezone.utc) + timedelta(seconds=TIMEOUT_SECONDS)
                            await message.author.timeout(
                                until,
                                reason="Too many non-numeric messages in verify channel",
                            )
                            log.warning("Discord-timeout applied to %s for %ds",
                                        message.author, TIMEOUT_SECONDS)
                        except discord.Forbidden:
                            log.warning("Could not timeout %s — missing Moderate Members perm "
                                        "or role hierarchy issue", message.author)
                        except discord.HTTPException as exc:
                            log.warning("timeout() failed for %s: %s", message.author, exc)

                    try:
                        await message.channel.send(
                            f"{message.author.mention} you've been timed out for "
                            f"**{TIMEOUT_SECONDS // 60} minutes** — please send only your "
                            "matricule (a number)."
                        )
                    except discord.HTTPException:
                        pass
                elif count == MAX_STRIKES - 1:
                    try:
                        await message.channel.send(
                            f"{message.author.mention} please send only your matricule (a number). "
                            f"One more non-numeric message and you'll be timed out for "
                            f"{TIMEOUT_SECONDS // 60} minutes."
                        )
                    except discord.HTTPException:
                        pass
                return

        extra = f" · {len(message.attachments)} attachment(s)" if message.attachments else ""
        log.info("[%s] #%s <%s>: %s%s",
                 message.guild.name, message.channel.name,
                 message.author, message.content, extra)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or before.content == after.content:
            return
        log.info("[%s] #%s <%s> edited: %r -> %r",
                 after.guild.name, after.channel.name,
                 after.author, before.content, after.content)
        embed = discord.Embed(
            title="✏️ Message modifié",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name="Salon", value=f"#{after.channel.name}", inline=False)
        embed.add_field(name="Avant", value=before.content[:1000] or "*(vide)*", inline=False)
        embed.add_field(name="Après", value=after.content[:1000] or "*(vide)*", inline=False)
        embed.set_footer(text=f"ID utilisateur : {after.author.id}")
        await _send_message_log(self, "", after.guild, embed=embed)

    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        log.info("[%s] #%s <%s> deleted: %r",
                 message.guild.name, message.channel.name, message.author, message.content)
        content = message.content[:1000] if message.content else "*(pas de texte / contenu non caché)*"
        embed = discord.Embed(
            title="🗑️ Message supprimé",
            description=content,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Salon", value=f"#{message.channel.name}", inline=False)
        embed.set_footer(text=f"ID utilisateur : {message.author.id}")
        await _send_message_log(self, "", message.guild, embed=embed)

    async def on_member_join(self, member: discord.Member):
        log.info("Member joined: %s (%s) in guild %s", member, member.id, member.guild.name)
        channel_id = _get_channel_id(self.config, "welcome_channel_id")
        if channel_id is None:
            return
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            log.warning("Welcome channel %s not found", channel_id)
            return

        try:
            buf = await _generate_welcome_image(self, member)
            file = discord.File(buf, filename="welcome.png")
            await channel.send(
                content=f"🎉 Bienvenue {member.mention} sur **{member.guild.name}** ! "
                        f"Va vite te vérifier dans <#{_get_channel_id(self.config, 'verify_channel_id')}> 🎓"
                        if self.config.get("verify_channel_id") else
                        f"🎉 Bienvenue {member.mention} sur **{member.guild.name}** !",
                file=file,
            )
        except Exception as exc:
            log.warning("Failed to generate/send welcome image for %s: %s", member, exc)
            try:
                await channel.send(f"🎉 Bienvenue {member.mention} sur **{member.guild.name}** !")
            except discord.HTTPException:
                pass

    async def on_member_remove(self, member: discord.Member):
        log.info("Member left: %s (%s) from guild %s", member, member.id, member.guild.name)
        embed = discord.Embed(
            title="🚪 Un membre a quitté le serveur",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="ID Discord", value=f"`{member.id}`", inline=True)
        if member.joined_at:
            embed.add_field(
                name="Avait rejoint le",
                value=discord.utils.format_dt(member.joined_at, "R"),
                inline=True,
            )
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        if roles:
            embed.add_field(name="Rôles qu'il avait", value=", ".join(roles), inline=False)
        mat = self._find_matricule_for_user(member.id)
        if mat:
            student = self.students.get(mat)
            if student:
                embed.add_field(
                    name="🎓 Était lié au matricule",
                    value=f"`{mat}` — {student.full_name}",
                    inline=False,
                )
        embed.set_footer(text=f"ID utilisateur : {member.id}")
        await _send_message_log(self, "", member.guild, embed=embed)

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        before_roles = set(before.roles)
        after_roles = set(after.roles)
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if not added and not removed:
            return

        # Try to find who made the change via the audit log (needs "View Audit Log" perm).
        actor = None
        try:
            async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                if entry.target and entry.target.id == after.id:
                    age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                    if age < 15:
                        actor = entry.user
                    break
        except discord.Forbidden:
            log.warning("Missing 'View Audit Log' permission — can't tell who changed roles for %s", after)
        except discord.HTTPException as exc:
            log.warning("Audit log lookup failed for %s: %s", after, exc)

        embed = discord.Embed(
            title="🔧 Rôles modifiés",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(after), icon_url=after.display_avatar.url)
        if added:
            embed.add_field(name="➕ Rôle(s) ajouté(s)", value=", ".join(r.mention for r in added), inline=False)
        if removed:
            embed.add_field(name="➖ Rôle(s) retiré(s)", value=", ".join(r.mention for r in removed), inline=False)
        embed.add_field(
            name="Par",
            value=str(actor) if actor else "Inconnu (probablement une action du bot, ou permission « Voir le journal d'audit » manquante)",
            inline=False,
        )
        embed.set_footer(text=f"ID utilisateur : {after.id}")
        await _send_message_log(self, "", after.guild, embed=embed)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.application_command:
            return
        cmd_name = interaction.command.name if interaction.command else "?"
        if cmd_name not in NO_LOG_COMMANDS:
            opts = ""
            if interaction.data.get("options"):
                opts = " [" + ", ".join(
                    f"{o['name']}={o.get('value')}" for o in interaction.data["options"]
                ) + "]"
            await _send_log(
                self,
                f"⚙️ `/{cmd_name}` par {interaction.user.mention} (`{interaction.user.id}`){opts}",
                interaction.guild,
            )
        log.info("Command /%s by %s (%s) in %s",
                 cmd_name, interaction.user, interaction.user.id,
                 interaction.guild.name if interaction.guild else "DM")

    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command):
        log.info("Command /%s completed for %s", command.name, interaction.user)
        if command.name not in NO_LOG_COMMANDS:
            await _send_log(
                self,
                f"✅ `/{command.name}` terminée pour {interaction.user.mention}",
                interaction.guild,
            )

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        cmd_name = interaction.command.name if interaction.command else "?"
        log.exception("Error in command /%s for %s: %s", cmd_name, interaction.user, error)
        await _send_log(
            self,
            f"🚨 **Erreur** dans `/{cmd_name}` par {interaction.user.mention} (`{interaction.user.id}`) : `{error}`",
            interaction.guild,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Une erreur est survenue. Les admins ont été notifiés.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Une erreur est survenue. Les admins ont été notifiés.", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _send_startup_message(self):
        sent = 0
        channel_ids = self.config.get("startup_channel_ids")
        if not channel_ids and self.config.get("mod_channel_id"):
            channel_ids = [self.config["mod_channel_id"]]
        for channel_id in channel_ids or []:
            try:
                cid = int(channel_id)
            except (TypeError, ValueError):
                continue
            channel = self.get_channel(cid)
            if channel is None:
                log.warning("Startup channel %s not found", channel_id)
                continue
            try:
                await channel.send(
                    f"🟢 **{self.user} is online.** "
                    f"Loaded **{len(self.students)}** students · "
                    f"Slash commands ready."
                )
                sent += 1
            except discord.HTTPException as exc:
                log.warning("Could not send startup message to channel %s: %s", channel_id, exc)
        await _send_log(self, f"🟢 **{self.user} is online.** Loaded **{len(self.students)}** students · Slash commands ready.")
        log.info("Startup message sent to %d channel(s)", sent)

    # ---------- student list ----------

    def reload_students(self):
        xlsx = BASE_DIR / self.config.get("xlsx_file", "students.xlsx")
        self.students, self.skipped_rows = load_students(str(xlsx))
        log.info("Loaded %d students from %s (%d rows skipped)",
                 len(self.students), xlsx.name, self.skipped_rows)

    def _find_matricule_for_user(self, user_id: int):
        return self.verified.get(str(user_id))

    def _find_user_for_matricule(self, matricule: str):
        return self.matricule_to_user.get(matricule)

    def _track_message(self, message: discord.Message):
        """Increment this user's tracked message count and record their last message."""
        uid = str(message.author.id)
        entry = self.message_stats.get(uid, {"count": 0})
        entry["count"] = entry.get("count", 0) + 1
        entry["last_content"] = (message.content or "*(pas de texte / pièce jointe seule)*")[:300]
        entry["last_channel"] = message.channel.name if hasattr(message.channel, "name") else "?"
        entry["last_channel_id"] = message.channel.id
        entry["last_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.message_stats[uid] = entry
        save_message_stats(self.message_stats)

    # ---------- commands ----------

    def _register_commands(self):
        cfg = self.config
        verified_role = cfg.get("verified_role", "MIV Verified")
        unverified_role = cfg.get("unverified_role", "MIV Unverified")

        # --------- Clic droit sur un membre → Voir informations ---------

        @self.tree.context_menu(name="Voir informations")
        async def voir_informations(interaction: discord.Interaction, member: discord.Member):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "❌ Réservé aux admins.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Fetch the raw User object too — it carries banner/accent_color,
            # which the cached Member object doesn't always have.
            try:
                full_user = await self.fetch_user(member.id)
            except discord.HTTPException:
                full_user = None

            embed = discord.Embed(
                title=f"Informations sur {member.display_name}",
                color=member.color if member.color.value else discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
                url=f"https://discord.com/users/{member.id}",
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if full_user and full_user.banner:
                embed.set_image(url=full_user.banner.url)

            # ---- Identité ----
            embed.add_field(name="Nom d'utilisateur", value=str(member), inline=True)
            if getattr(member, "global_name", None) and member.global_name != member.name:
                embed.add_field(name="Nom affiché (global)", value=member.global_name, inline=True)
            if member.nick:
                embed.add_field(name="Pseudo sur ce serveur", value=member.nick, inline=True)
            embed.add_field(name="ID Discord", value=f"`{member.id}`", inline=True)
            embed.add_field(name="Bot ?", value="Oui" if member.bot else "Non", inline=True)
            embed.add_field(name="Système ?", value="Oui" if member.system else "Non", inline=True)

            # ---- Avatar / bannière ----
            embed.add_field(name="Avatar", value=f"[Lien]({member.display_avatar.url})", inline=True)
            if full_user and full_user.accent_color:
                embed.add_field(name="Couleur d'accent", value=str(full_user.accent_color), inline=True)
            if member.avatar and member.guild_avatar:
                embed.add_field(
                    name="Avatar spécifique au serveur",
                    value=f"[Lien]({member.guild_avatar.url})",
                    inline=True,
                )

            # ---- Badges ----
            flags = [f.name for f in member.public_flags.all()] if member.public_flags else []
            if full_user and full_user.bot:
                pass
            if flags:
                embed.add_field(name="🏅 Badges", value=", ".join(flags), inline=False)

            # ---- Statut / activité (nécessite l'intent Presence activé) ----
            status_map = {
                discord.Status.online: "🟢 En ligne",
                discord.Status.idle: "🌙 Inactif",
                discord.Status.dnd: "⛔ Ne pas déranger",
                discord.Status.offline: "⚫ Hors ligne / invisible",
            }
            embed.add_field(name="Statut", value=status_map.get(member.status, "Non disponible (active l'intent Presence)"), inline=True)
            if member.activity:
                embed.add_field(name="Activité", value=str(member.activity.name), inline=True)

            # ---- Vocal ----
            if member.voice and member.voice.channel:
                vstate = []
                if member.voice.self_mute or member.voice.mute:
                    vstate.append("micro coupé")
                if member.voice.self_deaf or member.voice.deaf:
                    vstate.append("casque coupé")
                embed.add_field(
                    name="🔊 Actuellement en vocal",
                    value=f"{member.voice.channel.name}" + (f" ({', '.join(vstate)})" if vstate else ""),
                    inline=False,
                )

            # ---- Dates ----
            embed.add_field(
                name="Compte créé le",
                value=discord.utils.format_dt(member.created_at, "F") + " (" + discord.utils.format_dt(member.created_at, "R") + ")",
                inline=False,
            )
            if member.joined_at:
                embed.add_field(
                    name="A rejoint le serveur le",
                    value=discord.utils.format_dt(member.joined_at, "F") + " (" + discord.utils.format_dt(member.joined_at, "R") + ")",
                    inline=False,
                )
            if member.premium_since:
                embed.add_field(
                    name="💎 Booste le serveur depuis",
                    value=discord.utils.format_dt(member.premium_since, "F"),
                    inline=True,
                )
            if member.pending:
                embed.add_field(name="Vérification serveur", value="⏳ En attente (écran de bienvenue non validé)", inline=True)

            # ---- Rôles & permissions ----
            roles = [r.mention for r in member.roles if r.name != "@everyone"]
            embed.add_field(
                name=f"Rôles ({len(roles)})",
                value=", ".join(roles) if roles else "*aucun*",
                inline=False,
            )
            key_perms = []
            perms = member.guild_permissions
            for perm_name, label in [
                ("administrator", "Administrateur"), ("manage_guild", "Gérer le serveur"),
                ("manage_roles", "Gérer les rôles"), ("manage_channels", "Gérer les salons"),
                ("kick_members", "Expulser"), ("ban_members", "Bannir"),
                ("moderate_members", "Timeout"), ("manage_messages", "Gérer les messages"),
            ]:
                if getattr(perms, perm_name, False):
                    key_perms.append(label)
            if key_perms:
                embed.add_field(name="🔑 Permissions clés", value=", ".join(key_perms), inline=False)

            if member.timed_out_until:
                embed.add_field(
                    name="⏳ Actuellement timeout jusqu'à",
                    value=discord.utils.format_dt(member.timed_out_until, "F"),
                    inline=False,
                )

            # ---- Activité sur le serveur (suivie par le bot) ----
            stats = self.message_stats.get(str(member.id))
            if stats:
                embed.add_field(
                    name="💬 Messages envoyés (depuis que le bot compte)",
                    value=str(stats.get("count", 0)),
                    inline=True,
                )
                try:
                    last_at = datetime.fromisoformat(stats["last_at"])
                    last_at_str = discord.utils.format_dt(last_at, "R")
                except Exception:
                    last_at_str = "?"
                embed.add_field(
                    name="Dernier message",
                    value=(
                        f"#{stats.get('last_channel', '?')} · {last_at_str}\n"
                        f"> {stats.get('last_content', '')}"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="💬 Messages envoyés",
                    value="Aucun message suivi pour l'instant (le comptage a démarré au dernier redémarrage du bot).",
                    inline=False,
                )

            # ---- Fiche étudiant ----
            mat = self._find_matricule_for_user(member.id)
            if mat:
                student = self.students.get(mat)
                if student:
                    etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
                    embed.add_field(
                        name="🎓 Fiche étudiant (vérifié)",
                        value=(
                            f"**Nom :** {student.full_name}\n"
                            f"**Matricule :** `{student.matricule}`\n"
                            f"**Palier :** {student.palier}\n"
                            f"**Spécialité :** {student.specialite}\n"
                            f"**Section :** {student.section}\n"
                            f"**Statut :** {student.etat} ({etat_label})\n"
                            f"**Groupe TD :** {student.groupe_td or '?'} · **Groupe TP :** {student.groupe_tp or '?'}"
                        ),
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="🎓 Fiche étudiant",
                        value=f"Lié au matricule `{mat}` mais introuvable dans la base actuelle.",
                        inline=False,
                    )
            else:
                embed.add_field(name="🎓 Fiche étudiant", value="Non vérifié / pas de matricule lié.", inline=False)

            embed.set_footer(text=f"Demandé par {interaction.user}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        # --------- /verify ---------

        @self.tree.command(name="verify", description="Check your matricule and get verified as an MIV student")
        @app_commands.describe(matricule="Your university matricule number")
        async def verify(interaction: discord.Interaction, matricule: str):
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message("Please run this in a server channel.", ephemeral=True)
                return

            verify_channel_id = _get_channel_id(self.config, "verify_channel_id")
            if verify_channel_id is None:
                await interaction.response.send_message(
                    "Verification channel is not configured. Contact an admin.", ephemeral=True
                )
                return
            if interaction.channel.id != verify_channel_id:
                await interaction.response.send_message(
                    f"❌ Please use `/verify` in <#{verify_channel_id}> only.",
                    ephemeral=True,
                )
                log.info(
                    "Blocked /verify from %s in #%s (correct channel: %s)",
                    interaction.user, interaction.channel.name, verify_channel_id,
                )
                return

            uid = interaction.user.id
            uname = str(interaction.user)
            dname = getattr(interaction.user, "display_name", str(interaction.user))

            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message(
                    "That doesn't look like a matricule. A matricule is a number (e.g. `222231378114`).",
                    ephemeral=True,
                )
                return

            # Ack immediately: everything below is network I/O (roles, DM, logs).
            await interaction.response.defer(ephemeral=True)

            existing_owner_id = self._find_user_for_matricule(mat)
            if existing_owner_id is not None and existing_owner_id != str(uid):
                owner_mention = f"<@{existing_owner_id}>"
                log.warning(
                    "Duplicate matricule %s: %s (%s) tried to claim it, already owned by %s",
                    mat, uname, uid, existing_owner_id,
                )
                log_verification(uid, uname, dname, mat, status="ALREADY_VERIFIED")
                await _notify_mods(
                    self, interaction, mat, None,
                    f"duplicate attempt (owner: <@{existing_owner_id}>)",
                )
                await _send_log(
                    self,
                    f"🚨 **Doublon** — {interaction.user.mention} (`{uid}`) a tenté `{mat}` déjà lié à <@{existing_owner_id}>",
                    interaction.guild,
                )
                await _send_dm(interaction.user, f"❌ Le matricule `{mat}` est **déjà lié** à un autre compte Discord (<@{existing_owner_id}>).")
                await interaction.followup.send("❌ Ce matricule est déjà utilisé. Détails envoyés en DM.", ephemeral=True)
                return

            student = self.students.get(mat)

            if student is None:
                await _assign_visitor_roles(self, interaction.user, reason="matricule not in list")
                log_verification(uid, uname, dname, mat, status="NOT_IN_LIST")
                await _notify_mods(self, interaction, mat, None, "not in list")
                await _send_log(
                    self,
                    f"🚨 **Matricule inconnu** — {interaction.user.mention} (`{uid}`) via `/verify` → `{mat}`",
                    interaction.guild,
                )
                await _send_dm(interaction.user, f"❌ Le matricule `{mat}` ne figure **pas** sur la liste officielle des étudiants MIV Student.\nSi tu penses que c'est une erreur, contacte un administrateur.")
                await interaction.followup.send("❌ Vérification échouée. Détails envoyés en DM.", ephemeral=True)
                return

            if not student.is_sii:
                await _assign_visitor_roles(self, interaction.user, reason="wrong speciality")
                log_verification(
                    uid, uname, dname, mat,
                    full_name=student.full_name, section=student.section,
                    td=student.groupe_td, tp=student.groupe_tp,
                    status="WRONG_SPECIALITY",
                )
                await _notify_mods(self, interaction, mat, student, "wrong speciality")
                await _send_log(
                    self,
                    f"🚨 **Mauvaise spécialité** — {interaction.user.mention} (`{uid}`) via `/verify` → `{mat}` → {student.full_name}",
                    interaction.guild,
                )
                await _send_dm(interaction.user, f"❌ Le matricule `{mat}` correspond à **{student.full_name}** mais la spécialité est **{student.specialite}**, pas MIV Student.")
                await interaction.followup.send("❌ Vérification échouée. Détails envoyés en DM.", ephemeral=True)
                return

            if not student.is_admis:
                await _assign_visitor_roles(self, interaction.user, reason="not admis")
                log_verification(
                    uid, uname, dname, mat,
                    full_name=student.full_name, section=student.section,
                    td=student.groupe_td, tp=student.groupe_tp,
                    status="NOT_ADMIS",
                )
                etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
                await _notify_mods(self, interaction, mat, student, f"etat={student.etat}")
                await _send_log(
                    self,
                    f"🚨 **Non admis** — {interaction.user.mention} (`{uid}`) via `/verify` → `{mat}` → {student.full_name}",
                    interaction.guild,
                )
                await _send_dm(interaction.user, f"❌ Le matricule `{mat}` correspond à **{student.full_name}** mais ton statut est **{etat_label}** (non admis).\nSeuls les étudiants **Admis (ADM)** sont vérifiés. Contacte l'administration.")
                await interaction.followup.send("❌ Vérification échouée. Détails envoyés en DM.", ephemeral=True)
                return

            # Instead of finalizing immediately, ask for confirmation via DM buttons —
            # same flow as the #verify channel: the student must confirm it's really them
            # before any role is assigned.
            sent = await _send_dm(
                interaction.user,
                f"🎓 **Confirmation d'identité**\n"
                f"Le matricule **`{mat}`** correspond à :\n"
                f"**{student.full_name}** — {student.palier} {student.specialite} (Section {student.section})\n"
                f"Groupe TD : `{student.groupe_td or '?'}` · Groupe TP : `{student.groupe_tp or '?'}`\n\n"
                f"Confirme que c'est bien toi pour être vérifié.",
                view=VerifyConfirmView(self, uid, mat, student),
            )
            if not sent:
                await _send_log(
                    self,
                    f"⚠️ **DM inaccessible** — {interaction.user.mention} (`{uid}`) via `/verify` → `{mat}` mais ses DM sont fermés.",
                    interaction.guild,
                )
                await _notify_mods(self, interaction, mat, student, "DM fermés — vérification impossible")
                await interaction.followup.send(
                    "❌ Je n'ai pas pu t'envoyer de DM. Active tes messages privés (Confidentialité du serveur) puis relance `/verify`.",
                    ephemeral=True,
                )
                return

            await _send_log(
                self,
                f"📨 **Matricule reçu via /verify** — {interaction.user.mention} (`{uid}`) a soumis `{mat}` → {student.full_name}. DM de confirmation envoyé.",
                interaction.guild,
            )
            await interaction.followup.send(
                "📨 Vérifie ton **DM** et clique sur **« C'est bien moi »** pour finaliser ta vérification.",
                ephemeral=True,
            )

        # --------- /mes_infos ---------

        @self.tree.command(name="mes_infos", description="Show your own student record")
        async def mes_infos(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(
                    "Your matricule is no longer in the database. Contact a moderator.",
                    ephemeral=True,
                )
                return

            etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
            await interaction.response.send_message(
                f"🎓 **Your student record**\n"
                f"**Name:** {student.full_name}\n"
                f"**Matricule:** `{student.matricule}`\n"
                f"**Palier:** {student.palier}\n"
                f"**Spécialité:** {student.specialite}\n"
                f"**Section:** {student.section}\n"
                f"**Statut:** {student.etat} ({etat_label})\n"
                f"**Groupe TD:** `{student.groupe_td or '?'}` · **Groupe TP:** `{student.groupe_tp or '?'}`",
                ephemeral=True,
            )

        # --------- /mes_groupes ---------

        @self.tree.command(name="mes_groupes", description="Show your TD and TP groups")
        async def mes_groupes(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message("Your record was not found.", ephemeral=True)
                return

            await interaction.response.send_message(
                f"📚 **Your groups**\n"
                f"TD: **{student.groupe_td or '?'}** *(assigned as a role)*\n"
                f"TP: **{student.groupe_tp or '?'}** *(informational)*\n"
                f"Section: **{student.section}** · Palier: **{student.palier}**",
                ephemeral=True,
            )

        # --------- /mes_camarades ---------

        @self.tree.command(name="mes_camarades", description="List classmates in your TD group")
        async def mes_camarades(interaction: discord.Interaction):
            mat = self._find_matricule_for_user(interaction.user.id)
            if mat is None:
                await interaction.response.send_message(
                    "I don't know your matricule yet. Run `/verify <matricule>` first.",
                    ephemeral=True,
                )
                return
            me = self.students.get(mat)
            if me is None:
                await interaction.response.send_message("Your record was not found.", ephemeral=True)
                return

            group_num = me.groupe_td
            if not group_num:
                await interaction.response.send_message("You don't have a TD group assigned.", ephemeral=True)
                return

            classmates = [
                s for s in self.students.values()
                if s.is_sii
                and s.section == me.section
                and s.palier == me.palier
                and s.groupe_td == group_num
                and s.matricule != mat
            ]
            classmates.sort(key=lambda s: s.nom.lower())

            if not classmates:
                await interaction.response.send_message(
                    f"You're the only one in **TD {group_num}** (Section {me.section}).",
                    ephemeral=True,
                )
                return

            lines = [f"`{s.matricule}` — {s.full_name}" for s in classmates[:40]]
            header = (
                f"👥 **TD {group_num}** — Section {me.section} · "
                f"{len(classmates)} classmate(s)"
            )
            if len(classmates) > 40:
                header += " (showing first 40)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /groupe (admin-only) ---------

        @self.tree.command(name="groupe", description="List ALL students in a specific TD group (admin only)")
        @app_commands.describe(numero="TD group number (e.g. 1)")
        async def groupe(interaction: discord.Interaction, numero: str):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "❌ This command is restricted to admins. Use `/mes_camarades` instead.",
                    ephemeral=True,
                )
                return

            num = numero.strip()
            matches = [
                s for s in self.students.values()
                if s.is_sii and s.groupe_td == num
            ]
            matches.sort(key=lambda s: (s.section, s.nom.lower()))

            if not matches:
                await interaction.response.send_message(
                    f"No MIV students in **TD {num}**.", ephemeral=True
                )
                return

            lines = [f"`{s.matricule}` — {s.full_name} (Section {s.section})" for s in matches[:80]]
            header = f"👥 **TD {num}** — {len(matches)} student(s)"
            if len(matches) > 80:
                header += " (showing first 80)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /annuaire ---------

        @self.tree.command(name="annuaire", description="Look up a classmate's groups by matricule")
        @app_commands.describe(matricule="Classmate's matricule")
        async def annuaire(interaction: discord.Interaction, matricule: str):
            if not _is_verified_member(self, interaction.user) and not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "You must be verified to use this command. Run `/verify` first.",
                    ephemeral=True,
                )
                return
            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message("Please provide a numeric matricule.", ephemeral=True)
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(f"`{mat}` not found.", ephemeral=True)
                return
            if not student.is_sii:
                await interaction.response.send_message(f"`{mat}` is not an MIV student.", ephemeral=True)
                return

            await interaction.response.send_message(
                f"👤 **{student.full_name}**\n"
                f"Palier: {student.palier} · Section: **{student.section}**\n"
                f"TD: **{student.groupe_td or '?'}** · TP: **{student.groupe_tp or '?'}**",
                ephemeral=True,
            )

        # --------- /info ---------

        @self.tree.command(name="info", description="Show class info (planning, homework, etc.)")
        @app_commands.choices(topic=[
            app_commands.Choice(name="Planning", value="planning"),
            app_commands.Choice(name="Devoirs", value="devoirs"),
            app_commands.Choice(name="Règlement", value="reglement"),
        ])
        async def info(interaction: discord.Interaction, topic: app_commands.Choice[str]):
            msgs = self.config.get("info_messages", {})
            text = msgs.get(topic.value)
            if not text:
                await interaction.response.send_message(
                    f"No info available for **{topic.name}** yet.", ephemeral=True
                )
                return
            await interaction.response.send_message(text, ephemeral=True)

        # --------- /email ---------

        DEFAULT_EMAILS_TEXT = (
            "📧 **Contacts des responsables de modules**\n\n"
            "**Mail de MR.Benchaiba** responsable du cours/TD/TP du module SE :\n"
            "mabenchaiba@gmail.com\n\n"
            "**Mail de Mme.Djiroune** responsable du cours du module ABDD :\n"
            "djirahma@yahoo.fr\n\n"
            "**Mail de Mme.Sebai** responsable du cours/TP du module RP :\n"
            "meriem.sebai.ms@gmail.com\n\n"
            "**Mail de Mme.Mebtouche** responsable du cours/TD du module TAI :\n"
            "nawel.meb.5@gmail.com\n\n"
            "**Mail de Mme.Bensaou** responsable du cours du module Algo :\n"
            "bensaou.nacera@gmail.com\n\n"
            "**Mail de MR.Djouada** responsable du TD du module Algo :\n"
            "moussa.nadjib@gmail.com\n\n"
            "**Mail de MR.Larabi** responsable du cours du module multimédia :\n"
            "slimane.larabi@gmail.com"
        )

        @self.tree.command(name="email", description="Affiche les mails des responsables de modules")
        async def email_cmd(interaction: discord.Interaction):
            text = self.config.get("emails_message", DEFAULT_EMAILS_TEXT)
            await interaction.response.send_message(text, ephemeral=True)

        # --------- /edt ---------

        @self.tree.command(name="edt", description="Affiche l'emploi du temps de la promo")
        async def edt(interaction: discord.Interaction):
            filename = self.config.get("edt_image", "edt.png")
            path = BASE_DIR / filename
            if not path.exists():
                await interaction.response.send_message(
                    "❌ Aucun emploi du temps n'a encore été configuré. "
                    "Un admin peut en ajouter un avec `/edt_set`.",
                    ephemeral=True,
                )
                return
            try:
                file = discord.File(str(path), filename=path.name)
                await interaction.response.send_message(file=file, ephemeral=True)
            except Exception as exc:
                log.warning("Failed to send edt image: %s", exc)
                await interaction.response.send_message(
                    "❌ Impossible d'envoyer l'image de l'emploi du temps. Contacte un admin.",
                    ephemeral=True,
                )

        # --------- /edt_set (admin) ---------

        @self.tree.command(name="edt_set", description="Met à jour l'image de l'emploi du temps (admin)")
        @app_commands.describe(image="L'image de l'emploi du temps (PNG/JPG)")
        async def edt_set(interaction: discord.Interaction, image: discord.Attachment):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            if not (image.content_type or "").startswith("image/"):
                await interaction.response.send_message(
                    "❌ Le fichier envoyé n'est pas une image.", ephemeral=True
                )
                return

            filename = self.config.get("edt_image", "edt.png")
            ext = Path(image.filename).suffix or ".png"
            filename = str(Path(filename).with_suffix(ext))
            path = BASE_DIR / filename

            await interaction.response.defer(ephemeral=True)
            try:
                await image.save(path)
            except Exception as exc:
                await interaction.followup.send(f"❌ Échec de la sauvegarde : {exc}", ephemeral=True)
                return

            if filename != self.config.get("edt_image"):
                self.config["edt_image"] = filename
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(self.config, f, indent=4, ensure_ascii=False)
                except Exception as exc:
                    log.warning("Could not persist edt_image to config.json: %s", exc)

            await interaction.followup.send(
                f"✅ Emploi du temps mis à jour (`{filename}`). Teste avec `/edt`.", ephemeral=True
            )
            await _send_log(
                self,
                f"🗓️ **Emploi du temps mis à jour** par {interaction.user.mention}",
                interaction.guild,
            )

        # --------- /welcome_set (admin) ---------

        @self.tree.command(name="welcome_set", description="Met à jour le fond de l'image de bienvenue (admin)")
        @app_commands.describe(image="L'image de fond pour le message de bienvenue (PNG/JPG)")
        async def welcome_set(interaction: discord.Interaction, image: discord.Attachment):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            if not (image.content_type or "").startswith("image/"):
                await interaction.response.send_message(
                    "❌ Le fichier envoyé n'est pas une image.", ephemeral=True
                )
                return

            filename = self.config.get("welcome_background", "welcome_bg.png")
            ext = Path(image.filename).suffix or ".png"
            filename = str(Path(filename).with_suffix(ext))
            path = BASE_DIR / filename

            await interaction.response.defer(ephemeral=True)
            try:
                await image.save(path)
            except Exception as exc:
                await interaction.followup.send(f"❌ Échec de la sauvegarde : {exc}", ephemeral=True)
                return

            if filename != self.config.get("welcome_background"):
                self.config["welcome_background"] = filename
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(self.config, f, indent=4, ensure_ascii=False)
                except Exception as exc:
                    log.warning("Could not persist welcome_background to config.json: %s", exc)

            await interaction.followup.send(
                f"✅ Fond de bienvenue mis à jour (`{filename}`). Teste avec `/welcome_test`.", ephemeral=True
            )

        # --------- /welcome_test (admin) ---------

        @self.tree.command(name="welcome_test", description="Prévisualiser le message de bienvenue (admin)")
        async def welcome_test(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            channel_id = _get_channel_id(self.config, "welcome_channel_id")
            if channel_id is None:
                await interaction.response.send_message(
                    "❌ `welcome_channel_id` n'est pas configuré dans `config.json`.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                buf = await _generate_welcome_image(self, interaction.user)
                file = discord.File(buf, filename="welcome_preview.png")
                await interaction.followup.send("Aperçu de l'image de bienvenue :", file=file, ephemeral=True)
            except Exception as exc:
                await interaction.followup.send(f"❌ Erreur de génération : {exc}", ephemeral=True)

        # --------- /rappel ---------

        @self.tree.command(name="rappel", description="Set a personal reminder — the bot will DM you at that time")
        @app_commands.describe(
            date="When to remind you (e.g. `2026-09-25 14:30`, `25/09/2026 14:30`, or `2026-09-25`)",
            message="What to remind you about",
        )
        async def rappel(interaction: discord.Interaction, date: str, message: str):
            when = _parse_reminder_date(date)
            if when is None:
                await interaction.response.send_message(
                    "❌ Couldn't parse that date. Try formats like:\n"
                    "`2026-09-25 14:30` · `25/09/2026 14:30` · `2026-09-25` (defaults to 09:00 UTC)",
                    ephemeral=True,
                )
                return

            now = datetime.now(timezone.utc)
            if when <= now:
                await interaction.response.send_message(
                    "❌ That date is in the past. Pick a future time.", ephemeral=True
                )
                return
            if (when - now).days > REMINDER_MAX_DAYS_AHEAD:
                await interaction.response.send_message(
                    f"❌ Reminders can only be set up to {REMINDER_MAX_DAYS_AHEAD} days ahead.",
                    ephemeral=True,
                )
                return

            uid_str = str(interaction.user.id)
            user_count = sum(1 for r in self.reminders if r.get("user_id") == uid_str)
            if user_count >= MAX_REMINDERS_PER_USER:
                await interaction.response.send_message(
                    f"❌ You already have {MAX_REMINDERS_PER_USER} pending reminders. "
                    "Cancel some with `/annuler_rappel` first.",
                    ephemeral=True,
                )
                return

            msg = message.strip()
            if len(msg) > 500:
                msg = msg[:500] + "…"

            rid = uuid.uuid4().hex[:8]
            entry = {
                "id": rid,
                "user_id": uid_str,
                "username": str(interaction.user),
                "message": msg,
                "when": when.isoformat(timespec="seconds"),
                "created_at": now.isoformat(timespec="seconds"),
            }
            self.reminders.append(entry)
            save_reminders(self.reminders)

            unix = int(when.timestamp())
            await interaction.response.send_message(
                f"⏰ Reminder set for <t:{unix}:F> (<t:{unix}:R>).\n"
                f"**ID:** `{rid}` · **Message:** {msg}\n"
                "_The bot will DM you at that time. Make sure your DMs are open._",
                ephemeral=True,
            )
            log.info("Reminder %s set by %s for %s", rid, interaction.user, entry["when"])

        # --------- /mes_rappels ---------

        @self.tree.command(name="mes_rappels", description="List your pending reminders")
        async def mes_rappels(interaction: discord.Interaction):
            uid_str = str(interaction.user.id)
            mine = [r for r in self.reminders if r.get("user_id") == uid_str]
            if not mine:
                await interaction.response.send_message(
                    "You have no pending reminders.", ephemeral=True
                )
                return
            mine.sort(key=lambda r: r.get("when", ""))
            lines = []
            for r in mine[:20]:
                try:
                    when = datetime.fromisoformat(r["when"])
                    unix = int(when.timestamp())
                    when_str = f"<t:{unix}:F> (<t:{unix}:R>)"
                except Exception:
                    when_str = r.get("when", "?")
                msg = r.get("message", "")
                if len(msg) > 80:
                    msg = msg[:80] + "…"
                lines.append(f"`{r['id']}` — {when_str} — {msg}")
            header = f"⏰ **Your reminders** ({len(mine)})"
            if len(mine) > 20:
                header += " — showing first 20"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /annuler_rappel ---------

        @self.tree.command(name="annuler_rappel", description="Cancel one of your reminders by ID")
        @app_commands.describe(id="The reminder ID (see `/mes_rappels`)")
        async def annuler_rappel(interaction: discord.Interaction, id: str):
            uid_str = str(interaction.user.id)
            rid = id.strip()
            target = None
            for r in self.reminders:
                if r.get("id") == rid:
                    target = r
                    break
            if target is None:
                await interaction.response.send_message(
                    f"No reminder with ID `{rid}`.", ephemeral=True
                )
                return
            if target.get("user_id") != uid_str and not _is_admin(self, interaction.user):
                await interaction.response.send_message(
                    "❌ That reminder isn't yours.", ephemeral=True
                )
                return
            self.reminders = [r for r in self.reminders if r.get("id") != rid]
            save_reminders(self.reminders)
            await interaction.response.send_message(f"✅ Reminder `{rid}` cancelled.", ephemeral=True)

        # --------- /tous_les_rappels (admin) ---------

        @self.tree.command(name="tous_les_rappels", description="List every pending reminder, from every user (admin)")
        async def tous_les_rappels(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            if not self.reminders:
                await interaction.response.send_message("Aucun rappel en attente.", ephemeral=True)
                return
            ordered = sorted(self.reminders, key=lambda r: r.get("when", ""))
            lines = []
            for r in ordered[:40]:
                try:
                    when = datetime.fromisoformat(r["when"])
                    unix = int(when.timestamp())
                    when_str = f"<t:{unix}:f> (<t:{unix}:R>)"
                except Exception:
                    when_str = r.get("when", "?")
                msg = (r.get("message", "") or "")[:60]
                lines.append(f"`{r['id']}` — <@{r.get('user_id')}> — {when_str} — {msg}")
            header = f"⏰ **{len(self.reminders)} rappel(s) en attente au total**"
            if len(self.reminders) > 40:
                header += " (affichage des 40 premiers, triés par date)"
            await interaction.response.send_message(
                header + "\n" + "\n".join(lines) + "\n\nUtilise `/annuler_rappel <id>` pour en annuler un.",
                ephemeral=True,
            )

        # --------- /signaler ---------

        @self.tree.command(name="signaler", description="Report a problem to the moderators")
        @app_commands.describe(
            message="What's the problem?",
            anonymous="Hide your name from mods (default: no)",
        )
        async def signaler(interaction: discord.Interaction, message: str, anonymous: bool = False):
            report_channel_id = (
                _get_channel_id(self.config, "report_channel_id")
                or _get_channel_id(self.config, "mod_channel_id")
            )
            if report_channel_id is None:
                await interaction.response.send_message(
                    "Reporting is not configured. Contact an admin.", ephemeral=True
                )
                return
            channel = interaction.guild.get_channel(report_channel_id)
            if channel is None:
                await interaction.response.send_message(
                    "Report channel not found. Contact an admin.", ephemeral=True
                )
                return

            msg = message.strip()
            if len(msg) > 1500:
                msg = msg[:1500] + "…"

            reporter = "anonymous" if anonymous else f"{interaction.user} (`{interaction.user.id}`)"
            header = f"📨 **New report** from {reporter}"
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                header += f"\nChannel: {interaction.channel.mention}"

            embed = discord.Embed(
                title="Student report",
                description=msg,
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"Guild: {interaction.guild.name}")

            try:
                await channel.send(content=header, embed=embed)
            except discord.HTTPException as exc:
                log.warning("Could not deliver report: %s", exc)
                await interaction.response.send_message(
                    "Could not deliver your report. Please contact a mod directly.", ephemeral=True
                )
                return

            log.info("Report from %s: %s", interaction.user, msg[:100])
            await interaction.response.send_message(
                "✅ Your report has been sent to the moderators. Thank you.",
                ephemeral=True,
            )

        # --------- /backup ---------

        @self.tree.command(name="backup", description="Send data files to the mod channel (admin)")
        async def backup(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            files = []
            missing = []
            for path, label in [
                (VERIFIED_PATH, "verified.json"),
                (VERIFY_LOG_PATH, "verify_log.csv"),
                (CONFIG_PATH, "config.json"),
                (REMINDERS_PATH, "reminders.json"),
            ]:
                if path.exists():
                    try:
                        files.append(discord.File(str(path), filename=label))
                    except Exception as exc:
                        missing.append(f"{label} ({exc})")
                else:
                    missing.append(label)

            if not files:
                await interaction.followup.send(
                    "No files to back up: " + ", ".join(missing), ephemeral=True
                )
                return

            mod_channel_id = _get_channel_id(self.config, "mod_channel_id")
            channel = interaction.guild.get_channel(mod_channel_id) if mod_channel_id else None

            if channel is None:
                await interaction.followup.send(
                    f"Mod channel not found — sending here instead.\n"
                    + (f"Missing: {', '.join(missing)}" if missing else ""),
                    files=files,
                    ephemeral=True,
                )
            else:
                try:
                    await channel.send(
                        content=f"💾 **Backup** requested by {interaction.user.mention}",
                        files=files,
                    )
                    await interaction.followup.send(
                        f"✅ Backup sent to {channel.mention}."
                        + (f"\nMissing: {', '.join(missing)}" if missing else ""),
                        ephemeral=True,
                    )
                except discord.HTTPException as exc:
                    log.warning("Backup delivery failed: %s", exc)
                    await interaction.followup.send(
                        f"Could not deliver backup: {exc}", ephemeral=True
                    )

        # --------- /reload_config ---------

        @self.tree.command(name="reload_config", description="Re-read config.json without restart (admin)")
        async def reload_config(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            try:
                fresh = load_config_file()
            except Exception as exc:
                await interaction.response.send_message(
                    f"❌ Failed to read config.json: {exc}", ephemeral=True
                )
                return

            old_keys = set(self.config.keys())
            new_keys = set(fresh.keys())
            changed = []
            added = sorted(new_keys - old_keys)
            removed = sorted(old_keys - new_keys)
            for k in sorted(new_keys & old_keys):
                if self.config[k] != fresh[k]:
                    changed.append(k)

            self.config = fresh
            log.info("Config reloaded: +%s -%s ~%s", added, removed, changed)

            summary = ["🔄 **Config reloaded**"]
            if added:
                summary.append(f"➕ Added keys: `{', '.join(added)}`")
            if removed:
                summary.append(f"➖ Removed keys: `{', '.join(removed)}`")
            if changed:
                summary.append(f"✏️ Changed keys: `{', '.join(changed)}`")
            if not (added or removed or changed):
                summary.append("No changes detected.")

            summary.append(
                "\n*Note: `xlsx_file` and role names are re-read at next use. "
                "Run `/refresh` to reload the student list, and re-run `/sync_groupes` if roles changed.*"
            )

            await interaction.response.send_message("\n".join(summary), ephemeral=True)

        # --------- /timeout ---------

        @self.tree.command(name="timeout", description="Apply a real Discord timeout to a member (admin)")
        @app_commands.describe(
            user="Member to time out",
            minutes="Duration in minutes (1–10080)",
            reason="Reason shown in the audit log",
        )
        async def timeout_cmd(interaction: discord.Interaction, user: discord.Member,
                              minutes: int, reason: str = "Manual timeout"):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            if minutes < 1 or minutes > 10080:
                await interaction.response.send_message(
                    "Minutes must be between 1 and 10080 (7 days).", ephemeral=True
                )
                return
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await user.timeout(until, reason=f"{reason} (by {interaction.user})")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't time out that user — check my role hierarchy and "
                    "the `Moderate Members` permission.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"❌ Timeout failed: {exc}", ephemeral=True)
                return

            await interaction.response.send_message(
                f"🔇 Timed out {user.mention} for **{minutes} min**. Reason: {reason}",
                ephemeral=False,
            )

        # --------- /untimeout ---------

        @self.tree.command(name="untimeout", description="Remove a Discord timeout from a member (admin)")
        @app_commands.describe(user="Member to release")
        async def untimeout_cmd(interaction: discord.Interaction, user: discord.Member):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            try:
                await user.timeout(None, reason=f"Untimeout by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't modify that user's timeout.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"❌ Failed: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(f"🔊 {user.mention} is free again.", ephemeral=True)

        # --------- /creer_roles_groupes ---------

        @self.tree.command(name="creer_roles_groupes", description="Create G1..Gn roles from the list (admin)")
        async def creer_roles_groupes(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            fmt = cfg.get("group_role_format", "G{group}")
            groups = _all_td_groups(self.students)
            if not groups:
                await interaction.followup.send("No TD groups found in the student list.", ephemeral=True)
                return

            created, already, failed = [], [], []
            for g in groups:
                name = fmt.format(group=g)
                existing = discord.utils.get(interaction.guild.roles, name=name)
                if existing:
                    already.append(name)
                    continue
                try:
                    await interaction.guild.create_role(
                        name=name,
                        reason="Created by MIV verify bot",
                        mentionable=True,
                    )
                    created.append(name)
                except discord.Forbidden:
                    failed.append(name)
                except discord.HTTPException as exc:
                    log.warning("Failed to create role %s: %s", name, exc)
                    failed.append(name)

            msg = []
            if created:
                msg.append(f"✅ Created: {', '.join(f'`{n}`' for n in created)}")
            if already:
                msg.append(f"ℹ️ Already existed: {', '.join(f'`{n}`' for n in already)}")
            if failed:
                msg.append(f"❌ Failed: {', '.join(f'`{n}`' for n in failed)}")
            await interaction.followup.send("\n".join(msg) or "Nothing to do.", ephemeral=True)

        # --------- /sync_groupes ---------

        @self.tree.command(name="sync_groupes", description="Bulk-assign G* roles to verified members from the list (admin)")
        @app_commands.describe(dry_run="If true, only reports what would change without touching roles")
        async def sync_groupes(interaction: discord.Interaction, dry_run: bool = False):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            fmt = cfg.get("group_role_format", "G{group}")
            verified_role_name = cfg.get("verified_role")
            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role_name)

            if verified_role_obj is None:
                await interaction.followup.send(
                    f"Role `{verified_role_name}` doesn't exist on this server.", ephemeral=True
                )
                return

            stats = {
                "checked": 0, "not_linked": 0, "no_group": 0, "role_missing": 0,
                "assigned": 0, "already_ok": 0, "removed_stale": 0, "errors": 0,
            }
            details = []

            for member in verified_role_obj.members:
                stats["checked"] += 1
                mat = self.verified.get(str(member.id))
                if not mat:
                    stats["not_linked"] += 1
                    details.append(f"• {member.mention} — *no linked matricule*")
                    continue
                student = self.students.get(mat)
                if student is None:
                    stats["not_linked"] += 1
                    details.append(f"• {member.mention} — matricule `{mat}` not in DB")
                    continue
                if not student.groupe_td:
                    stats["no_group"] += 1
                    details.append(f"• {member.mention} — no TD group in list")
                    continue

                target_name = fmt.format(group=student.groupe_td)
                target_role = discord.utils.get(interaction.guild.roles, name=target_name)
                if target_role is None:
                    stats["role_missing"] += 1
                    details.append(f"• {member.mention} — role `{target_name}` missing")
                    continue

                group_prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
                current_group_roles = [
                    r for r in member.roles
                    if r.name.startswith(group_prefix)
                    and r.name[len(group_prefix):].isdigit()
                ]
                stale = [r for r in current_group_roles if r.name != target_name]

                if target_role in member.roles and not stale:
                    stats["already_ok"] += 1
                    continue

                if dry_run:
                    action = []
                    if stale:
                        action.append(f"remove {', '.join(r.name for r in stale)}")
                    if target_role not in member.roles:
                        action.append(f"add {target_name}")
                    details.append(f"• {member.mention} — would {'; '.join(action)}")
                    continue

                try:
                    if stale:
                        await member.remove_roles(*stale, reason="MIV sync")
                        stats["removed_stale"] += len(stale)
                    if target_role not in member.roles:
                        await member.add_roles(target_role, reason="MIV sync")
                        stats["assigned"] += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    stats["errors"] += 1
                    details.append(f"• {member.mention} — ERROR: {exc}")

            header = (
                f"**{'DRY RUN — ' if dry_run else ''}Group sync report**\n"
                f"Verified members scanned: **{stats['checked']}**\n"
                f"Roles assigned: **{stats['assigned']}** · already OK: **{stats['already_ok']}**\n"
                f"Stale roles removed: **{stats['removed_stale']}**\n"
                f"No matricule link: **{stats['not_linked']}** · no TD group: **{stats['no_group']}**\n"
                f"Role missing on server: **{stats['role_missing']}** · errors: **{stats['errors']}**"
            )

            detail_text = ""
            if details:
                shown = details[:30]
                detail_text = "\n\n" + "\n".join(shown)
                if len(details) > 30:
                    detail_text += f"\n… and {len(details) - 30} more"

            await interaction.followup.send(header + detail_text, ephemeral=True)

        # --------- /verify_log ---------

        @self.tree.command(name="verify_log", description="Download the verification log as CSV (admin)")
        @app_commands.describe(last="Only include the last N entries (default 200, 0 = all)")
        async def verify_log_cmd(interaction: discord.Interaction, last: int = 200):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            if not VERIFY_LOG_PATH.exists():
                await interaction.response.send_message("No verifications logged yet.", ephemeral=True)
                return

            with open(VERIFY_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if len(lines) <= 1:
                await interaction.response.send_message("Log file is empty.", ephemeral=True)
                return

            header_row = lines[0]
            body = lines[1:]
            total = len(body)
            if last and last > 0:
                body = body[-last:]

            content = header_row + "".join(body)
            data = content.encode("utf-8")
            file = discord.File(io.BytesIO(data), filename="verify_log.csv")
            await interaction.response.send_message(
                f"📋 Verification log — **{len(body)}** entries shown (of {total} total).",
                file=file,
                ephemeral=True,
            )

        # --------- /check ---------

        @self.tree.command(name="check", description="Look up any matricule in the database")
        @app_commands.describe(matricule="Matricule to look up")
        async def check(interaction: discord.Interaction, matricule: str):
            mat = matricule.strip().replace(" ", "")
            if not mat.isdigit():
                await interaction.response.send_message("Please provide a numeric matricule.", ephemeral=True)
                return
            student = self.students.get(mat)
            if student is None:
                await interaction.response.send_message(f"`{mat}`: not found in the database.", ephemeral=True)
                return
            etat_label = ETAT_LABELS.get(student.etat.upper(), student.etat)
            await interaction.response.send_message(
                f"**{student.full_name}** — {student.palier} {student.specialite} (Section {student.section})\n"
                f"Matricule: `{student.matricule}` · Status: **{student.etat}** ({etat_label})\n"
                f"Groupe TD: {student.groupe_td or '?'} · TP: {student.groupe_tp or '?'}",
                ephemeral=True,
            )

        # --------- /sondage ---------

        @self.tree.command(name="sondage", description="Créer un sondage avec réactions emoji")
        @app_commands.describe(
            question="La question du sondage",
            option1="Option 1 (laisse vide pour un sondage Oui/Non simple)",
            option2="Option 2",
            option3="Option 3",
            option4="Option 4",
            option5="Option 5",
        )
        async def sondage(interaction: discord.Interaction, question: str,
                          option1: str = None, option2: str = None,
                          option3: str = None, option4: str = None, option5: str = None):
            options = [o for o in [option1, option2, option3, option4, option5] if o]

            if not options:
                embed = discord.Embed(
                    title="📊 Sondage",
                    description=f"**{question}**",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text=f"Sondage créé par {interaction.user}")
                await interaction.response.send_message(embed=embed)
                msg = await interaction.original_response()
                for emoji in ("👍", "👎"):
                    await msg.add_reaction(emoji)
                return

            if len(options) < 2:
                await interaction.response.send_message(
                    "❌ Donne au moins 2 options, ou aucune pour un sondage Oui/Non simple.",
                    ephemeral=True,
                )
                return

            number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
            lines = [f"{number_emojis[i]} {opt}" for i, opt in enumerate(options)]
            embed = discord.Embed(
                title="📊 Sondage",
                description=f"**{question}**\n\n" + "\n".join(lines),
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"Sondage créé par {interaction.user}")
            await interaction.response.send_message(embed=embed)
            msg = await interaction.original_response()
            for i in range(len(options)):
                await msg.add_reaction(number_emojis[i])

        # --------- /classement ---------

        @self.tree.command(name="classement", description="Top 10 des étudiants les plus actifs (messages)")
        async def classement(interaction: discord.Interaction):
            if not self.message_stats:
                await interaction.response.send_message(
                    "Aucune activité suivie pour l'instant (le comptage démarre au lancement du bot).",
                    ephemeral=True,
                )
                return

            ranked = sorted(
                self.message_stats.items(),
                key=lambda kv: kv[1].get("count", 0),
                reverse=True,
            )[:10]

            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, (uid, data) in enumerate(ranked):
                rank = medals[i] if i < 3 else f"`#{i + 1}`"
                mat = self._find_matricule_for_user(int(uid))
                student = self.students.get(mat) if mat else None
                name_suffix = f" — {student.full_name}" if student else ""
                lines.append(f"{rank} <@{uid}>{name_suffix} — **{data.get('count', 0)}** messages")

            embed = discord.Embed(
                title="🏆 Classement — Membres les plus actifs",
                description="\n".join(lines),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Basé sur les messages suivis depuis le dernier démarrage du bot")
            await interaction.response.send_message(embed=embed)

        # --------- /stats ---------

        @self.tree.command(name="stats", description="Show bot statistics")
        async def stats(interaction: discord.Interaction):
            total = len(self.students)
            sii = sum(1 for s in self.students.values() if s.is_sii)
            admis = sum(1 for s in self.students.values() if s.is_admis)
            sii_admis = sum(1 for s in self.students.values() if s.is_sii and s.is_admis)

            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
            verified_count = len(verified_role_obj.members) if verified_role_obj else 0

            groups = _all_td_groups(self.students)

            log_entries = 0
            if VERIFY_LOG_PATH.exists():
                try:
                    with open(VERIFY_LOG_PATH, "r", encoding="utf-8") as f:
                        log_entries = max(0, sum(1 for _ in f) - 1)
                except Exception:
                    pass

            await interaction.response.send_message(
                f"📊 **Bot Statistics**\n"
                f"**Students loaded:** {total}\n"
                f"**MIV students:** {sii}\n"
                f"**Admis (all):** {admis}\n"
                f"**MIV + Admis (verifiable):** {sii_admis}\n"
                f"**Verified members in this server:** {verified_count}\n"
                f"**Rows skipped in xlsx:** {self.skipped_rows}\n"
                f"**Linked accounts (verified.json):** {len(self.verified)}\n"
                f"**Verification log entries:** {log_entries}\n"
                f"**Pending reminders:** {len(self.reminders)}\n"
                f"**TD groups in list:** {', '.join(groups) if groups else 'none'}",
                ephemeral=True,
            )

        # --------- /search ---------

        @self.tree.command(name="search", description="Search for a student by name or matricule (admin)")
        @app_commands.describe(query="Partial name or matricule (case-insensitive)")
        async def search(interaction: discord.Interaction, query: str):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            q = query.strip().lower()
            if len(q) < 2:
                await interaction.response.send_message("Enter at least 2 characters.", ephemeral=True)
                return

            matches = [
                s for s in self.students.values()
                if q in s.full_name.lower() or q in s.matricule.lower()
            ][:15]

            if not matches:
                await interaction.response.send_message(f"No students match `{query}`.", ephemeral=True)
                return

            lines = [
                f"`{s.matricule}` — **{s.full_name}** · {s.palier} {s.specialite} · {s.etat}"
                for s in matches
            ]
            header = f"🔎 **{len(matches)} result(s)** for `{query}`"
            if len(matches) == 15:
                header += " (showing first 15)"
            await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

        # --------- /refresh ---------

        @self.tree.command(name="refresh", description="Reload the student list from the xlsx file (admin)")
        async def refresh(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return
            try:
                self.reload_students()
            except Exception as exc:
                await interaction.response.send_message(f"Failed to reload: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Reloaded **{len(self.students)}** students from the xlsx file.", ephemeral=True
            )

        # --------- /unverify ---------

        @self.tree.command(name="unverify", description="Remove verification from a user (admin)")
        @app_commands.describe(user="Member to unverify")
        async def unverify(interaction: discord.Interaction, user: discord.Member):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            verified_role_obj = discord.utils.get(interaction.guild.roles, name=verified_role)
            unverified_role_obj = discord.utils.get(interaction.guild.roles, name=unverified_role)

            if verified_role_obj is None:
                await interaction.response.send_message(f"Role `{verified_role}` not found.", ephemeral=True)
                return

            removed = []
            try:
                if verified_role_obj in user.roles:
                    await user.remove_roles(verified_role_obj, reason=f"Unverified by {interaction.user}")
                    removed.append(f"-{verified_role_obj.name}")
                if unverified_role_obj and unverified_role_obj not in user.roles:
                    await user.add_roles(unverified_role_obj, reason=f"Unverified by {interaction.user}")
                    removed.append(f"+{unverified_role_obj.name}")

                fmt = cfg.get("group_role_format", "G{group}")
                prefix = fmt.split("{group}")[0] if "{group}" in fmt else "G"
                group_roles = [
                    r for r in user.roles
                    if r.name.startswith(prefix) and r.name[len(prefix):].isdigit()
                ]
                if group_roles:
                    await user.remove_roles(*group_roles, reason=f"Unverified by {interaction.user}")
                    removed.extend(f"-{r.name}" for r in group_roles)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I don't have permission to modify that user's roles.", ephemeral=True
                )
                return

            if str(user.id) in self.verified:
                old_mat = self.verified[str(user.id)]
                self.matricule_to_user.pop(old_mat, None)
                del self.verified[str(user.id)]
                save_verified(self.verified)

            await interaction.response.send_message(
                f"✅ Unverified {user.mention}. Changes: {', '.join(removed) or 'none'}",
                ephemeral=True,
            )

        # --------- /export ---------

        @self.tree.command(name="export", description="Export the student list as CSV (admin)")
        async def export(interaction: discord.Interaction):
            if not _is_admin(self, interaction.user):
                await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                return

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Matricule", "Nom", "Prénom", "Palier", "Spécialité", "Section", "État", "TD", "TP"])
            for s in sorted(self.students.values(), key=lambda x: x.matricule):
                writer.writerow([
                    s.matricule, s.nom, s.prenom, s.palier, s.specialite,
                    s.section, s.etat, s.groupe_td, s.groupe_tp,
                ])

            data = buf.getvalue().encode("utf-8")
            file = discord.File(io.BytesIO(data), filename="students_export.csv")
            await interaction.response.send_message(
                f"📁 Export of **{len(self.students)}** students.", file=file, ephemeral=True
            )

        # --------- /list_commands ---------

        @self.tree.command(name="list_commands", description="Debug: show registered commands")
        async def list_commands(interaction: discord.Interaction):
            try:
                cmds = await self.tree.fetch_commands(guild=interaction.guild)
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Fetch failed: {exc}", ephemeral=True)
                return
            if not cmds:
                await interaction.response.send_message(
                    "No commands registered. The bot is missing the `applications.commands` scope — re-invite.",
                    ephemeral=True,
                )
                return
            names = "\n".join(f"- `/{c.name}` — {c.description}" for c in cmds)
            await interaction.response.send_message(f"Registered commands:\n{names}", ephemeral=True)

        # --------- /help ---------

        @self.tree.command(name="help", description="Show available commands")
        async def help_cmd(interaction: discord.Interaction):
            await interaction.response.send_message(
                "**📖 Available commands**\n\n"
                "**For students**\n"
                "`/verify <matricule>` — Verify yourself (only in the verify channel)\n"
                "`/mes_infos` — Show your full student record\n"
                "`/mes_groupes` — Show your TD and TP groups\n"
                "`/mes_camarades` — List classmates in your TD group\n"
                "`/annuaire <matricule>` — Look up a classmate's groups\n"
                "`/rappel <date> <message>` — Set a personal reminder (DM'd to you)\n"
                "`/mes_rappels` — List your reminders\n"
                "`/annuler_rappel <id>` — Cancel a reminder\n"
                "`/signaler <message>` — Send a report to the mod team\n"
                "`/info <topic>` — Planning, homework, rules\n"
                "`/edt` — Voir l'emploi du temps\n"
                "`/email` — Voir les mails des responsables de modules\n"
                "`/sondage <question> [options...]` — Créer un sondage\n"
                "`/classement` — Top 10 des membres les plus actifs\n"
                "`/stats` — Bot statistics\n"
                "`/help` — This message\n\n"
                "**Admins only**\n"
                "`/welcome_set <image>` — Fond du message de bienvenue\n"
                "`/welcome_test` — Prévisualiser le message de bienvenue\n"
                "`/groupe <n>` — List ALL students in TD n\n"
                "`/timeout <user> <min>` · `/untimeout <user>`\n"
                "`/edt_set <image>` — Mettre à jour l'emploi du temps\n"
                "`/tous_les_rappels` — Voir tous les rappels en attente\n"
                "`/backup` · `/reload_config` · `/creer_roles_groupes` · `/sync_groupes` · `/verify_log`\n"
                "`/check` · `/search` · `/refresh` · `/unverify` · `/export` · `/list_commands`",
                ephemeral=True,
            )


# ---------------- entry point ----------------

def main():
    if not CONFIG_PATH.exists():
        log.error("config.json not found. Copy config.example.json to config.json and fill it in.")
        raise SystemExit(1)
    config = load_config_file()

    token = os.environ.get("DISCORD_TOKEN") or config.get("discord_token")
    if not token:
        log.error("No Discord token. Set DISCORD_TOKEN env var or discord_token in config.json")
        raise SystemExit(1)

    bot = VerifyBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()