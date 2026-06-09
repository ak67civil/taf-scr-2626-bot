# Premium Save Restricted Content Bot

This repository contains the source code for the Premium Save Restricted Content Bot. It allows users to clone content from private or public Telegram channels/groups to a destination of their choice.

## Features
- **Clone Content**: Copy messages, files, and media from private or public channels, even if forwarding is restricted.
- **Support for Thread/Topics**: Supports both normal groups and groups structured with forum topics.
- **Large File Support**: Transfer files up to 2GB+. Files larger than 1.9GB are automatically split.
- **Caption & Filename Modification**: Define custom rules to edit filenames, replace text in captions, remove specific text, or append extra text.
- **Custom Transfer Thumbnail**: Set a custom thumbnail to be applied to all video files.
- **Dedicated Workers (Heroku Integration)**: Utilizes Heroku one-off dynos to spawn dedicated worker processes for each user's transfer. This provides isolated RAM and CPU, thus avoiding global rate limits and server crashes when multiple users are active.
- **Resume Capability**: Saves transfer progress to resume in case of interruptions.

---

## 🛠️ Configuration & Credentials

Before deploying, you need to gather the necessary credentials.

1. **Telegram API ID & API Hash**
   - Go to [my.telegram.org](https://my.telegram.org/).
   - Log in with your phone number.
   - Go to "API development tools" and create a new application to get your `API_ID` and `API_HASH`.

2. **Telegram Bot Token**
   - Go to Telegram and search for [@BotFather](https://t.me/BotFather).
   - Send `/newbot` and follow the steps.
   - Copy the HTTP API Token provided (this is your `BOT_TOKEN`).

3. **Admin ID**
   - This is your personal Telegram User ID. You can find it by messaging [@userinfobot](https://t.me/userinfobot) or similar bots.
   - Example: `123456789`.

4. **MongoDB URI**
   - Go to [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) and create a free account.
   - Create a new cluster and set up a database user with a password.
   - Click "Connect" -> "Connect your application" and copy the connection string.
   - Replace `<password>` with your actual database user password.
   - Your string should look like: `mongodb+srv://user:pass@cluster0.mongodb.net/?retryWrites=true&w=majority`.

5. **Heroku API Token & App Name**
   - Log in to your [Heroku Dashboard](https://dashboard.heroku.com/).
   - Create a new app (this will be your `HEROKU_APP_NAME`).
   - Go to your Account Settings (top right avatar -> Account Settings).
   - Scroll down to "API Key" and click "Reveal". Copy this key (this is your `HEROKU_API_TOKEN`).

6. **Heroku Dyno Size**
   - By default, the bot spawns `standard-1x` dynos for workers.
   - To give users more RAM (highly recommended for 2GB+ files), you can set `WORKER_DYNO_SIZE` to `standard-2x` (1GB RAM) or `performance-m` (2.5GB RAM). Note: These larger dynos cost more on Heroku.

---

## 🚀 Deployment on Heroku (Docker Based)

Because this bot requires system dependencies like FFmpeg to process videos, the recommended deployment method on Heroku is via **Docker containers**.

### Prerequisites
- Install [Git](https://git-scm.com/downloads).
- Install [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli).
- Install [Docker Desktop](https://www.docker.com/products/docker-desktop).

### Step-by-Step Deployment

1. **Login to Heroku CLI**
   Open your terminal/command prompt and run:
   ```bash
   heroku login
   heroku container:login
   ```

2. **Set Environment Variables (Config Vars)**
   Go to your Heroku App Dashboard -> **Settings** -> **Reveal Config Vars** and add all the following keys:
   - `API_ID` : (e.g., 123456)
   - `API_HASH` : (e.g., abcdef123456...)
   - `BOT_TOKEN` : (e.g., 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)
   - `MONGO_URI` : (Your MongoDB connection string)
   - `ADMIN_ID` : (Your personal Telegram ID)
   - `HEROKU_API_TOKEN` : (From Heroku Account Settings)
   - `HEROKU_APP_NAME` : (Your Heroku App Name exactly as it appears)
   - `OWNER_USERNAME` : (e.g., `@YourUsername` — displayed to users when they try to buy or need help)
   - `WORKER_DYNO_SIZE` : `standard-2x` (Recommended for heavy loads. Default is `standard-1x`)

3. **Push the Docker Container**
   In your terminal, navigate to the folder where this repository is located:
   ```bash
   cd path/to/Premium-SRC-Repo
   ```
   Push the Docker image to Heroku:
   ```bash
   heroku container:push web -a your-heroku-app-name
   ```
   *(This step might take several minutes as it builds the image and installs FFmpeg and Python dependencies.)*

4. **Release the Container**
   ```bash
   heroku container:release web -a your-heroku-app-name
   ```

5. **Start the Web Dyno**
   Go to your App Dashboard -> **Resources** tab, and ensure the `web` dyno is toggled ON. Alternatively, run:
   ```bash
   heroku ps:scale web=1 -a your-heroku-app-name
   ```

*(Note: The main bot runs on the `web` dyno. When a user starts a transfer, the bot automatically uses your `HEROKU_API_TOKEN` to spawn a temporary `worker` dyno for that specific user. Once the transfer finishes, the bot kills that worker dyno to save money.)*

---

## 👑 Admin Guide & Controls

As the bot owner, you manage subscriptions and monitor the system. All administrative commands must be sent to the bot from your personal Telegram account (the one matching `ADMIN_ID`).

### Managing Subscriptions
Since auto-payments are disabled, users who type `/buy` will see a message telling them to contact you directly (`OWNER_USERNAME`).

1. **User pays you manually** via UPI/Crypto/etc.
2. **You ask for their Telegram ID**. The user can get their ID by sending `/id` to the bot.
3. **Grant them access**:
   Send the `/add_user` command to the bot with their ID and the duration.
   **Syntax**: `/add_user <USER_ID> <DURATION>`
   **Examples**:
   - `/add_user 123456789 30d` *(Grants 30 days access)*
   - `/add_user 123456789 7d` *(Grants 7 days access)*
   - `/add_user 123456789 12h` *(Grants 12 hours access)*
4. The user instantly receives a DM from the bot confirming their subscription is active.

### Revoking Access
If you need to ban a user or remove their premium status:
- Send `/revoke 123456789`
- This immediately stops any active transfers they have running and revokes their access to `/clone`.

### Viewing Users
- `/users` : Displays a list of ALL users registered in the database.
- `/paid_users` : Displays a list of currently active premium users and the hours left on their subscription.

### Setting Up the Logs Channel
You can set a Telegram channel where the bot will send updates (like when a file is successfully transferred).
1. Create a private Telegram channel.
2. Add your bot as an **Administrator** with posting rights.
3. Get the Channel ID. You can forward a message from that channel to [@userinfobot] or Rose bot to get the ID (it will look like `-1001234567890`).
4. Send to your bot: `/set_log -1001234567890`

### Monitoring & Managing Dynos
Because the bot spawns Heroku dynos for each user, you can monitor them:
- `/dynos` : Opens a panel showing all currently running worker dynos, who is running them, and their RAM usage.
- `/kill_dyno <dyno_name>` : Force kills a stuck dyno (e.g., `/kill_dyno run.1234`).

### Other Admin Commands
- `/broadcast` : Reply to any message (text, photo, video) with `/broadcast` and the bot will forward it to every user in the database. Useful for announcements.
- `/extract_string` : Exports a text file containing the Telethon/Pyrogram session strings of all your logged-in users. Keep this extremely safe!

---

## 📚 User Experience Guide

Here is what your customers will experience:

1. **Purchase**: The user sends `/buy` and is told to contact your username.
2. **Login**: After you use `/add_user`, the user sends `/login`. The bot asks for their phone number (in international format, e.g., `+919876543210`) and sends them a Telegram OTP. They enter the OTP to log in.
3. **Setup Clone**:
   - User types `/clone`.
   - Bot asks for the link to the **First Message** (e.g., `https://t.me/c/1234567/100`).
   - Bot asks for the link to the **Last Message**.
   - Bot asks for the **Destination Channel ID** (where they want to save the files). The user must make sure their account is an admin in the destination.
4. **Settings Panel**: Before starting, the user is presented with options to:
   - Set a custom thumbnail.
   - Replace text in the caption or filename.
   - Remove text from the caption.
5. **Transfer**: The user clicks "Start". The bot informs them it is spawning a dedicated dyno. The transfer begins, and the user can type `/dyno_status` to monitor RAM usage, or `/stop` to cancel.
