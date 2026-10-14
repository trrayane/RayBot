import json, os

config = {
    "discord_token": os.environ.get("DISCORD_TOKEN", ""),
    "sync_guild_id": int(os.environ.get("SYNC_GUILD_ID", "0")),
    "admin_user_ids": [int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()],
    "admin_role": os.environ.get("ADMIN_ROLE", "Admin"),
    "verified_role": os.environ.get("VERIFIED_ROLE", "MIV"),
    "verified_role_id": None,
    "unverified_role": os.environ.get("UNVERIFIED_ROLE", "Visitor"),
    "unverified_role_id": None,
    "mod_channel_id": int(os.environ.get("MOD_CHANNEL_ID", "0")),
    "verify_channel_id": int(os.environ.get("VERIFY_CHANNEL_ID", "0")),
    "report_channel_id": int(os.environ.get("REPORT_CHANNEL_ID", "0")),
    "log_channel_id": int(os.environ.get("LOG_CHANNEL_ID", "0")),
    "welcome_channel_id": int(os.environ.get("WELCOME_CHANNEL_ID", "0")),
    "emails_message": os.environ.get("EMAILS_MESSAGE", ""),
    "startup_channel_ids": [int(x) for x in os.environ.get("STARTUP_CHANNEL_IDS", "").split(",") if x.strip()],
    "message_log_channel_id": int(os.environ.get("MESSAGE_LOG_CHANNEL_ID", "0")),
    "group_role_format": os.environ.get("GROUP_ROLE_FORMAT", "G{group}"),
    "group_role_ids": json.loads(os.environ.get("GROUP_ROLE_IDS", "{}")),
    "info_messages": json.loads(os.environ.get("INFO_MESSAGES", "{}")),
    "xlsx_file": os.environ.get("XLSX_FILE", "M1 IV A (Liste Affichage).xlsx"),
    "edt_image": os.environ.get("EDT_IMAGE", "edt.png"),
    "welcome_background": os.environ.get("WELCOME_BACKGROUND", "welcome_bg.jpg"),
}

with open(os.path.join(os.path.dirname(__file__), "config.json"), "w", encoding="utf-8") as f:
    json.dump(config, f, indent=4, ensure_ascii=False)

print("config.json generated from environment variables")
