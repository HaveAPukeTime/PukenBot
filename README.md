# Puken Redux — Quick Setup

Do NOT commit your bot token. Use the environment variable DISCORD_TOKEN or a local `.env` file.

Quick setup:
- Install dependencies: `pip install -r requirements.txt`
- Create `.env` from `.env.example` and set `DISCORD_TOKEN`.
- Run: `python Puken_Git.py`

Visual Studio 2022 debugging:
- Open your project, right-click the project → __Project Properties__.
- Go to the __Debugging__ (or __Debug__) tab.
- Add the environment variable: `DISCORD_TOKEN=your_bot_token_here`

Git:
- A `.gitignore` is provided to avoid committing environment files and runtime data.
- Commit `.env.example` (placeholder) but never commit `.env`.