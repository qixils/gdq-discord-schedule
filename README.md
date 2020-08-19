# gdq-discord-schedule

This is a Discord bot that creates a mirror of the GDQ schedule in a specified channel, complete with VOD links, runners, start times, and more.

It additionally posts an embed at the end of the channel listing the current run, the upcoming 3 runs, and when they will start.

The bot respects GDQ's rate limits as best as it can, with most of the requests coming from the initial run that loads runner names and Twitch URLs. Requests are minimal after that.

## Usage

Use `pip install -U -r requirements.txt` to install the required dependencies.

Set the global variables at the top of the file appropriately, particularly the event ID which is required to know which event to load.

Create a `token.txt` file with a Discord bot token contained within.

Run with `python3 main.py`
