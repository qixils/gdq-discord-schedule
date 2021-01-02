import asyncio
import datetime
import json
import re
import traceback
import pytz
import discord
import aiohttp
import humanize
from dateutil.parser import isoparse
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
from discord.ext import tasks


config = load(open('config.yaml', 'r'), Loader)

# local timezone for appropriately displaying when the upcoming run is
local_timezone = pytz.timezone(config['local_timezone'])

# request headers
gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}
reddit_headers = {"headers": {"User-Agent": "simple-wiki-reader:v0.1 (/u/noellekiq)"}}  # add your own reddit username here?

# aiohttp session, do not change
session: aiohttp.ClientSession = None  # gets defined later because it yelled at me for creating in non-async func
utc = pytz.timezone('UTC')

fix_space: re.Pattern = re.compile(" {2,}")

async def load_horaro_json(schedule: bool = True, ticker: bool = False):
    """
    Loads and processes a GDQ API page
    :param schedule: whether to get the schedule or base event page
    :param ticker: whether to grab the ticker or not
    :return: json object
    """
    query = ''
    if schedule:
        query += '/schedules'
    if ticker and schedule:
        query += f'/{ticker}/ticker'
    url = f"{config['gdq_url']}{config['event_id']}{query}"
    async with session.get(url, **gdq_headers) as r:
        if r.status == 200:
            jsondata = await r.json()

            # GDQ doesn't provide official ratelimits, so we apply our own safe amount
            await asyncio.sleep(2.5)
        else:
            print("GET {} returned {} {} -- aborting".format(url, r.status, await r.text()))
            exit()

    out = jsondata['data']
    if schedule:
        out = out[config['horaro_index']]

    return out


# Utility Functions
def comma_format(input_list):
    *a, b = input_list
    return ' and '.join([', '.join(a), b]) if a else b


def line_split(input_message, char_limit=2000):
    output = []
    for line in input_message.split('\n'):
        line = line.strip() + '\n'
        if output and len((output[-1] + line).strip()) <= char_limit:
            output[-1] += line
        else:
            while line:
                output.append(line[:char_limit])
                line = line[char_limit:]
    return [msg.strip() for msg in output]


class DiscordClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.author = "@lexikiq#0493"  # me, the bot creator :)

        self.social_emoji = {}  # emojis used for social media links
        self.runners = {}  # dict of runner_id: fields

        # start the background schedule processor
        self.processor.start()

    def get_time(self, timestamp: int):
        dt = datetime.datetime.utcfromtimestamp(timestamp).replace(tzinfo=utc)
        return dt.astimezone(self.timezone)

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

    async def on_message(self, message):
        if message.channel.id == config['schedule_channel'] and message.type == discord.MessageType.pins_add and \
                message.channel.permissions_for(message.guild.me).manage_messages:
            await message.delete()

    async def human_schedule(self):
        """
        Processes the human-readable schedule.
        :return: list of runs
        """
        # load pages
        index = await load_horaro_json()
        schedule = index['items']

        # Header Message
        o = [f"**{self.eventname}** ({index['name']})"]
        if 'description' in index and index['description']:
            o.append(index['description'])
        if 'website' in index and index['website']:
            o.append(f"Event website: <{index['website']}>")
        socials = []
        for skey in ['twitter', 'twitch']:
            if skey in index and index[skey]:
                socials.append(f"{self.social_emoji[skey]}/{index[skey]}")
        if socials:
            o.append('     '.join(socials))
        o.append(f"All times are in {self.timezone}.")
        outputmsg = '\n'.join(o)
        schedule_list = [outputmsg]

        current_date = datetime.date(year=1970, month=1, day=15)  # for splitting schedule by end of day

        # finally iterate through every run
        for run_data in schedule:
            starts_at = self.get_time(run_data['scheduled_t'])  # converts utc time to event time
            starts_at_frmt = starts_at.strftime("`%b %d %I:%M %p`")  # formats for msg later
            # adds the new day separator
            prefix = ''
            if starts_at.date() > current_date:
                prefix += fix_space.sub("", starts_at.strftime("_ _%n> **%A** %b %e%n_ _%n"))
                current_date = starts_at.date()

            game = run_data['data'][0]
            length = datetime.timedelta(seconds=run_data['length_t'])
            estimate = str(length)
            ends_at = starts_at + length

            dtnow = datetime.datetime.now(self.timezone)
            # upcoming games list (channel topic)
            gameslist_prefix = None
            # if one of the upcoming runs:
            if 0 < len(self.gameslist) < config['upcoming_runs']+1:
                htime = humanize.naturaltime(starts_at.astimezone(local_timezone).replace(tzinfo=None))
                gameslist_prefix = htime[0].upper() + htime[1:]  # capitalize first letter
            # if current run:
            elif starts_at <= dtnow < ends_at.astimezone(self.timezone):
                prefix += "\N{BLACK RIGHTWARDS ARROW} "
                gameslist_prefix = "Current Game"
            # if one of the above two if statements executed
            if gameslist_prefix:
                runline = f"{gameslist_prefix}: {game}"
                self.gameslist.append(runline)

            output = f"{prefix}{starts_at_frmt}: {game} in {estimate}"
            schedule_list.append(output)

        return schedule_list

    async def process_message(self, schedule, channel=None, message=None):
        """
        Edits or sends a new message to the schedule channel.
        :param schedule: A schedule list from human_schedule()
        :param channel: (optional) channel to send new messages to
        :param message: (optional) a discord Message to edit
        :return: None
        """
        if self.msgIndex >= len(schedule):
            if message is not None:
                await message.delete()  # idk if this check is necessary
            return
        outputmsg = schedule[self.msgIndex]
        is_embed = not isinstance(outputmsg, str)
        embed = None
        if is_embed:
            index = await load_horaro_json()
            twitch = index['twitch'] if 'twitch' in index and index['twitch'] else config['twitch_channel']
            s_name = "{} {}".format(self.social_emoji['twitch'], twitch).strip()
            desc = [f"Bot created by {self.author}",
                    f"Updates every {config['wait_minutes']} minutes",
                    f"Watch live at [{s_name}](https://twitch.tv/{twitch})"]
            embed = discord.Embed(title=f"{self.eventname} Run Roster",
                                  description='\n'.join(desc),
                                  timestamp=datetime.datetime.utcnow(), color=0x3bb830)
            embed.set_footer(text="Last updated:")
            if outputmsg:
                for run in outputmsg:
                    # from the self.gameslist, the messages take the format of "Current Run: Game (Category) by Runners"
                    run_when = run.split(':')[0].strip()
                    run_desc = ':'.join(run.split(':')[1:]).strip()
                    embed.add_field(name=run_when, value=run_desc, inline=False)
            else:
                val_end = "The event has ended. Thank you all for watching and donating!"
                val_strt = self.starttime.strftime("The event will start on %A %b %e.")
                _dt = datetime.datetime.utcnow().replace(tzinfo=utc).astimezone(self.timezone)
                val_bool = _dt > self.starttime

                val = val_end if val_bool else val_strt
                embed.add_field(name="N/A", value=val)
            outputmsg = None
        output_args = {False: {"args": [outputmsg], "kwargs": {}}, True: {"args": [], "kwargs": {"embed": embed}}}

        if message is None:
            data = output_args[is_embed]
            message = await channel.send(*data["args"], **data["kwargs"])
        else:
            if outputmsg is None or message.content != outputmsg.strip():
                await message.edit(content=outputmsg, embed=embed)

        if (outputmsg and outputmsg.startswith('\N{BLACK RIGHTWARDS ARROW}')) or self.msgIndex == 0:
            if not message.pinned:
                await message.pin()
        elif message.pinned:
            await message.unpin()

        self.msgIndex += 1
        return None

    @tasks.loop(minutes=config['wait_minutes'])
    async def processor(self):
        # donation status changer
        try:  # the SCHEDULE
            # reset variables
            self.gameslist = []
            self.msgIndex = 0
            # get schedule
            schedule = await self.human_schedule()
            schedule.append(self.gameslist)  # add data for embed

            dtoffset = self.starttime.astimezone(utc).replace(tzinfo=None) - datetime.timedelta(days=1)

            # update/post the schedule messages
            async for message in self.rushschd.history(after=dtoffset, limit=None):
                if message.author == self.user:
                    await self.process_message(schedule, message=message)
            while self.msgIndex < len(schedule):
                await self.process_message(schedule, channel=self.rushschd)
            print(f"{datetime.datetime.now()} Schedule Updated!")
        except Exception as e:
            print(f"SCHEDULE: {e}")
            traceback.print_exc()

        await self.rushschd.edit(topic='\n\n'.join(self.gameslist))

    @processor.before_loop
    async def before_processor(self):
        # load session
        global session
        session = aiohttp.ClientSession()
        index = await load_horaro_json(schedule=False)
        schedule = await load_horaro_json()
        self.eventname = index['name']
        self.timezone = pytz.timezone(schedule['timezone'])
        self.starttime = self.get_time(schedule['start_t'])

        # we've done everything we can do before discord is ready, now wait for discord.py to finish connecting
        await self.wait_until_ready()

        # load social media emojis
        for key, emoji in config['emojis'].items():
            if isinstance(emoji, int):
                disc_emoji = self.get_emoji(emoji)
                if disc_emoji:
                    emoji = str(disc_emoji)
                else:
                    emoji = ""
            self.social_emoji[key] = emoji

        # load embed author
        lexi = self.get_user(140564059417346049)
        if lexi:
            self.author = lexi.mention

        # get channel
        self.rushschd = self.get_channel(config['schedule_channel'])
        assert self.rushschd is not None


client = DiscordClient(allowed_mentions=discord.AllowedMentions.none())
client.run(config['token'], bot=True)
