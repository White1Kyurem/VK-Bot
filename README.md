# VK-Bot

A Discord bot for voice levels, welcome messages, and an automatic role when members join the server.

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
