# gdq-discord-schedule

This is a Discord bot that creates a mirror of the GDQ schedule in a specified text channel, complete with VOD links,
runners, start times, donation incentive progress, bid war options, and more.

It additionally posts an embed at the end of the channel listing the current run, the upcoming 3 runs (configurable), and when they will start.

The bot respects GDQ's rate limits as best as it can by waiting 2.5 seconds between each API call.
Requests are minimal as it only calls the list of runs, donation incentives/bid wars, bid war options, and runner information.

### main.py

This is the script that runs the schedule creator and updater with the above features.

### games.py

This is a very hardcoded side-project that runs various games related to the donation total of the event.
Most should probably ignore this.

## Usage

[Create a Discord bot](https://discord.com/developers/applications/) if you haven't already.

Use `pip install -U -r requirements.txt` to install the required dependencies.

Copy `example_config.yaml` to `config.yaml` and change the values as appropriate.
Detailed descriptions of each value are included in comments.

Run with `python3 main.py`
