# VK-Bot

A Discord bot for voice levels, welcome messages, and an automatic role when members join the server.

## Eight voice levels

The eight-level setup has five regular levels followed by three OG levels:

| Stage | Display name | Role variable | Total hours variable |
| --- | --- | --- | --- |
| 1–5 | Level 1–5 | `LEVEL_1_ROLE_ID` through `LEVEL_5_ROLE_ID` | `LEVEL_1_HOURS` through `LEVEL_5_HOURS` |
| 6 | OG Level 1 | `LEVEL_6_ROLE_ID` | `LEVEL_6_HOURS` |
| 7 | OG Level 2 | `LEVEL_7_ROLE_ID` | `LEVEL_7_HOURS` |
| 8 | OG Level 3 | `LEVEL_8_ROLE_ID` | `LEVEL_8_HOURS` |

To upgrade an existing installation, keep the current variables for Levels 1–5.
Create the three OG roles in Discord, then set the following in Railway Variables:

```dotenv
LEVEL_COUNT=8
LEVEL_6_ROLE_ID=YOUR_OG_LEVEL_1_ROLE_ID
LEVEL_6_HOURS=200
LEVEL_7_ROLE_ID=YOUR_OG_LEVEL_2_ROLE_ID
LEVEL_7_HOURS=350
LEVEL_8_ROLE_ID=YOUR_OG_LEVEL_3_ROLE_ID
LEVEL_8_HOURS=500
```

The values **200, 350, and 500 hours are examples**. Choose your own total-hour thresholds.
They must increase strictly, and `LEVEL_6_HOURS` must exceed the current `LEVEL_5_HOURS`.
Each OG role needs a unique numeric role ID. The bot's highest role must be above all eight
level roles and the join role.

Deploy the updated variables and code, check `/botstatus` for **Configured Levels: 8**,
then run `/levels-sync` to apply the correct roles from saved voice time.
The OG names appear in `/rank`, `/levels`, `/leaderboard`, level-up announcements, and logs.
With `ROLE_MODE=highest`, only the highest earned level role is kept. With `cumulative`,
all earned level roles are kept. The independent join role is preserved in either mode.

Saved voice time and awarded stages keep their existing numeric values; no database reset
or migration is needed. For compatibility, an existing installation without `LEVEL_COUNT`
still defaults to five levels until the eight-level configuration is supplied.

## Automatic join role

Add a variable under **Variables** in the bot's Railway service:

```dotenv
AUTO_ROLE_ID=YOUR_ROLE_ID
```

Enter the real numeric role ID only in Railway. In Discord, enable Developer Mode under
**User Settings → Advanced**, then go to **Server Settings → Roles**, right-click the
desired role, and select **Copy Role ID**.

The bot assigns this role as soon as a person joins the configured server.
It is added alongside existing roles and is kept during voice level synchronization.
Use a separate role that is not configured as a `LEVEL_x_ROLE_ID`.
Bots and existing server members are not assigned a role by this feature.
Role assignment also works when the welcomer is disabled.
Leaving `AUTO_ROLE_ID` empty or unset disables the feature.

The bot needs **Manage Roles**. Its highest role must be **above the join role** in the
role list. `@everyone` and managed roles cannot be used.
Administrators can use `/botstatus` to check the configuration and permissions.

## Discord connection

In the Discord Developer Portal under **Bot → Privileged Gateway Intents**, enable
**Server Members Intent** and click **Save Changes**.
Then restart the Railway service. The welcomer and join role both require this intent.

Startup logs show the bot's name, bot ID, application ID, and the direct portal link
for the application whose token Railway is actually using.
If an intent error still occurs, open that exact link and check the setting there.
You do not need to invite the bot again to change this setting.

## Configuration

The other variables for the server, levels, welcomer, and data storage are described in
`.env.example`. Store real tokens only in Railway Variables.
The SQLite database containing voice times must be stored on a Railway volume.

## Local checks

After installing the dependencies, run `python -m unittest discover -s tests -v`.
These checks do not connect to Discord or assign real roles.
