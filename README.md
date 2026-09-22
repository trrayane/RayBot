# 🎓 MIV Verify Bot

A production-grade Discord bot that verifies university students against the official
student list (MS Excel) and manages their roles automatically.

> **Purpose** — Confirms that each member is a genuine **M1 MIV Student**, grants them
> the correct roles (**MIV** + **TD group role**), and keeps out anyone who is not on
> the list.

---

## ✨ Features

| Feature | Description |
| --- | --- |
| 🔐 **Matricule verification** | Checks your matricule against the official `.xlsx` list (matricule, specialty, admission status) |
| 💬 **DM-first flow** | Verification runs entirely in DMs with a confirm/deny button — no personal data is ever posted in a public channel |
| 🎭 **Automatic roles** | Assigns the **MIV** role + the student's **TD group role** (`G1`…`G3`) on success; assigns **Visitor** on failure |
| 🛡️ **Duplicate prevention** | A matricule can only be linked to one Discord account (checked at input, at confirmation, and via `/verify`) |
| 🧾 **Full audit logging** | Startup, submissions, successes and failures are logged to a dedicated channel |
| 🚫 **Anti-spam** | 3 non-numeric tries in 60s → automatic 5‑minute timeout |
| ⏰ **Reminders** | Personal reminders delivered by DM (`/rappel`) |
| 📊 **Student toolkit** | `/mes_infos`, `/mes_groupes`, `/mes_camarades`, `/annuaire` |
| 🗄️ **Admin suite** | Role bulk‑sync, CSV exports, backups, timeouts, live reconfiguration |

---

## 📥 Getting started

### Prerequisites

- **Python 3.10+**
- A Discord **application** with a bot token
  (Developer Portal → *Applications* → *Bot* → *Reset Token*)
- The **server ID** of your guild
- The official student list as `.xlsx` (see [Configuration](#%EF%B8%8F-configuration))

### 1. Install

```bash
cd verify_bot
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configuration

```bash
cp config.example.json config.json
```

Edit `config.json` with **your** values:

| Key | Description |
| --- | --- |
| `discord_token` | Your bot token — **never share this file** |
| `sync_guild_id` | Your server (guild) ID |
| `admin_user_ids` | Discord IDs of bot owners |
| `admin_role` | Name of the administrator role |
| `verified_role` / `verified_role_id` | Role granted on success (name, and optionally its ID) |
| `unverified_role` / `unverified_role_id` | Role granted on failure (typically **Visitor**) |
| `verify_channel_id` | Channel where matricules are submitted |
| `log_channel_id` | Channel where all bot logs are written |
| `mod_channel_id` / `report_channel_id` | Channels for moderation alerts / reports |
| `startup_channel_ids` | Channels that receive the online message at startup |
| `group_role_ids` | Map TD group → role ID (`"1"` → role **G1**, etc.) |
| `group_role_format` | Naming pattern for group roles, e.g. `G{group}` |
| `xlsx_file` | Path to the official student list |

> ⚠️ **Security:** `config.json` contains your bot token. Add it to `.gitignore`
> and **never commit or paste it publicly**. If leaked, regenerate the token immediately.

### 3. Server roles

Create the following roles in your server and note their IDs:

- **MIV** — verified students
- **Visitor** — unverified / rejected members
- **G1**, **G2**, **G3** — TD group roles

Put the **bot's role above all of these** in the role hierarchy and grant the bot the
**Manage Roles** permission — otherwise assignment will fail.

### 4. Run

```bash
python bot.py
```

On startup the bot:

1. Syncs its slash commands to your server;
2. Sends a 🟢 *online* message to the startup channels;
3. Starts the reminder scheduler and listens for matricules in `verify_channel_id`.

Reload configuration without restarting:

```
/reload_config
```

---

## 🧑‍🎓 For students

### Verify yourself

Run in the verification channel:

```
/verify 222231378114
```

*(replace the number with the matricule printed on your student card)*

| Outcome | Result |
| --- | --- |
| ✅ On the list, MIV + Admis | DM confirmation → **MIV** role + your group role (e.g. `G2`) |
| ❌ Unknown / wrong specialty / not admitted | DM with the reason → **Visitor** role |

> Matricules can also be sent as a **plain message** in the verification channel —
> the bot deletes it and continues the check in DMs.
>
> 🚫 Do not write non-numeric spam in that channel: after **3 failed tries in 60 seconds**
> the bot will **time you out for 5 minutes**.

### Student commands

| Command | Description |
| --- | --- |
| `/verify <matricule>` | Verify your identity |
| `/mes_infos` | Your own student record |
| `/mes_groupes` | Your TD / TP groups |
| `/mes_camarades` | Everyone in your TD group |
| `/annuaire <matricule>` | A classmate's groups |
| `/rappel <date> <message>` | Personal reminder via DM |
| `/mes_rappels` | List your pending reminders |
| `/annuler_rappel <id>` | Cancel a reminder |
| `/signaler <message>` | Report an issue to the moderators |
| `/info <topic>` | Class info: `planning`, `devoirs`, `reglement` |
| `/help` | Show available commands |

---

## 🛠️ Administrator commands

| Command | Description |
| --- | --- |
| `/creer_roles_groupes` | Create the `G1`…`Gn` roles from the student list |
| `/sync_groupes` | Bulk-assign group roles to verified members |
| `/groupe <num>` | List all students of a TD group |
| `/check <matricule>` | Look up any matricule in the database |
| `/search <query>` | Search students by name or matricule |
| `/verify_log` | Download the verification log (CSV) |
| `/export` | Export the student list (CSV) |
| `/backup` | Send data files to the mod channel |
| `/reload_config` | Re-read `config.json` without restarting |
| `/refresh` | Reload the student list from the `.xlsx` file |
| `/timeout <user> <minutes>` | Apply a Discord timeout |
| `/untimeout <user>` | Remove a timeout |
| `/unverify <user>` | Remove a member's verification |
| `/stats` | Bot statistics |

---

## 📁 File layout

```
├── README.md
├── M1 SII A (Liste Affichage).xlsx   # Official student list (source of truth)
└── verify_bot/
    ├── bot.py                        # Main application
    ├── students.py                   # Student dataclass + list loader
    ├── requirements.txt              # Python dependencies
    ├── config.example.json           # Template (safe to commit)
    ├── config.json                   # Your secrets — DO NOT commit
    ├── verified.json                 # Discord user ↔ matricule bindings
    ├── verify_log.csv                # Every verification attempt
    └── reminders.json                # Pending reminders
```

---

## 🧩 How verification works

```
User sends a matricule (command or message)
        │
        ▼
┌─ bot validates input (numeric, correct channel) ─┐
│   ✗ spam → anti-spam count, 5-min timeout         │
│   ✗ duplicates → blocked + logged                 │
└───────────────────────────────────────────────────┘
        │
        ▼
┌─ student lookup in the .xlsx list ────────────────┐
│   ✗ not found ──────────────► Visitor + log       │
│   ✗ wrong specialty ────────► Visitor + log       │
│   ✗ not admitted (ETAT) ────► Visitor + log       │
│   ✓ matched ──► DM with profile + ✅ / ❌ buttons │
└───────────────────────────────────────────────────┘
        │ (confirmed)
        ▼
assign MIV role + TD group role   ·   remove Visitor
        │
        ▼
write verified.json   ·   log success   ·   notify moderators
```

Issues are imported, de-duplicated and matched on `Matricule`, `Spécialité`,
`Section`, `Groupe TD`, `Groupe TP` and admission status (`ETAT`).

---

## 🆘 Troubleshooting

**Roles are not assigned even though verification succeeds**
The bot's role is too low in the hierarchy or it lacks **Manage Roles**. Move the bot's
role above `MIV` / `Visitor` / `G1`–`G3` and re-check permissions, then try again.

**The bot says "application did not respond" on `/verify`**
The interaction was not acknowledged in time. Restart the bot; if it persists, check the
console logs for the exact error.

**Matricules are not found**
Verify that `xlsx_file` points to the correct file and re-load it with `/refresh`.

**Logs don't appear**
Confirm `log_channel_id` is the numeric ID of the intended channel and that the bot can
post there.

---

## 📄 License

Internal project — for the exclusive use of the **MIV** class administration.