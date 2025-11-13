# src/main.py

from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()

from .keep_alive import keep_alive
from .bot import bot, DISCORD_TOKEN


def main():
    # Start the tiny Flask keep-alive server (for Render / uptime pings)
    keep_alive()

    # Start the Discord bot
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
