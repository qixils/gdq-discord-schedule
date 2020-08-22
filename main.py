import asyncio
import datetime
import json
import traceback
import pytz
import discord
import aiohttp
import humanize
from dateutil.parser import *
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


config = load(open('config.yaml', 'r'), Loader)

# local timezone for appropriately displaying when the upcoming run is
local_timezone = pytz.timezone(config['local_timezone'])

# request headers
gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}
reddit_headers = {"headers": {"User-Agent": "simple-wiki-reader:v0.1 (/u/noellekiq)"}}  # add your own reddit username here?

# aiohttp session, do not change
session: aiohttp.ClientSession = None  # gets defined later because it yelled at me for creating in non-async func


async def load_gdq_json(query):
    """
    Loads and processes a GDQ API page
    :param query: the search parameters to query
    :return: json object
    """
    url = f"{config['gdq_url']}{query}"
    async with session.get(url, **gdq_headers) as r:
        if r.status == 200:
            jsondata = await r.json()

            # self-ratelimit to avoid bullying the API. impacts the first run the most as it fills in the username cache,
            # but further runs should only be affected by about 10 seconds.
            await asyncio.sleep(2.5)
        else:
            print("GET {} returned {} {} -- aborting".format(url, r.status, await r.text()))
            exit()
    return jsondata


async def load_gdq_index():
    """
    Returns the GDQ index (main) page, includes donation totals
    :return: json object
    """
    return (await load_gdq_json(f"?type=event&id={config['event_id']}"))[0]['fields']


async def load_json_from_reddit(wiki_page, subreddit="VODThread", log_errors: bool = True):
    """
    Reads json from a reddit wiki page. Allows the use of # as a comment character.
    stolen from https://github.com/blha303/gdq-scripts/blob/master/genvods.py
    :param wiki_page: the wiki page to check
    :param subreddit: the subreddit containing the wikipage
    :param log_errors: whether to print errors
    """
    url = f'https://www.reddit.com/r/{subreddit}/wiki/{wiki_page}.json'
    async with session.get(url, **reddit_headers) as r:
        if r.status == 200:
            jsondata = await r.json()
        else:
            if log_errors:
                print("GET {} returned {} {} -- ignoring".format(url, r.status, await r.text()))
            return []
    page = jsondata['data']['content_md'].replace("\r\n", "\n")
    wiki_data = "\n".join([line.partition("#")[0].rstrip() for line in page.split("\n")])
    return json.loads(wiki_data)


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
        self.event: str = None  # name of the event
        self.timezone: pytz.timezone = None  # timezone of the event
        self.runners = {}  # dict of runner_id: fields

        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.my_background_task())

    async def runner_name(self, runner_id):
        """
        Returns the corresponding runner username for a runner ID
        :param runner_id: integer for runner ID
        :return: returns runner fields
        """
        return self.runners[runner_id]

    async def human_schedule(self):
        """
        Processes the human-readable schedule.
        :return: list of runs
        """
        schedule = await load_gdq_json(f"?type=run&event={config['event_id']}")
        # Header Message
        index = await load_gdq_index()
        dnmsg1 = "Join the {dns} donators who have raised {amt} for {cha} at " \
                 "https://gamesdonequick.com/tracker/ui/donate/{lnk}. (Minimum Donation: {mnd})"
        dnmsg2 = "Raised {amt} from {dns} donators for {cha}. "
        dnmsg = dnmsg1 if index['allow_donations'] else dnmsg2
        dnmsg = dnmsg.format(dns=f"{int(index['count']):,}", amt=f"${float(index['amount']):,.2f}",
                             cha=index['receivername'], lnk=self.event, mnd=f"${float(index['minimumdonation']):,.2f}")
        outputmsg = '\n'.join([f"**{index['name']}**",
                               f"All times are in {self.timezone}.",
                               dnmsg])
        schedule_list = [outputmsg]

        gdqvods = await load_json_from_reddit(f'{self.event}vods')
        gdqytvods = await load_json_from_reddit(f'{self.event}yt', log_errors=False)
        runcount = 0
        bids = await load_gdq_json(f"?type=bid&event={config['event_id']}")
        biddex = {}
        current_date = datetime.date(year=1970, month=1, day=15)  # for splitting schedule by end of day
        # pre-prepare bids (more efficient than repeatedly iterating through every bid)
        for bidorigin in bids:
            if bidorigin['fields']['biddependency'] is not None:  # idk what this is, i suspect it's for bid options?
                continue  # but i've never seen it used...
            bidorigrunid = bidorigin['fields']['speedrun']
            if bidorigrunid not in biddex:
                biddex[bidorigrunid] = []
            biddex[bidorigrunid].append(bidorigin['fields'])
        # finally iterate through every run
        for run_data_base in schedule:
            run_data = run_data_base['fields']  # all run data contained in here (except the ID)

            starts_at = isoparse(run_data['starttime']).astimezone(self.timezone)  # converts utc time to event time
            starts_at_frmt = starts_at.strftime("`%b %d %I:%M %p`")  # formats for msg later
            # adds the new day separator
            prefix = ''
            if starts_at.date() > current_date:
                prefix += starts_at.strftime("_ _%n> **%A** %b %e%n_ _%n")
                current_date = starts_at.date()

            # name options/examples:
            #   'name': 'Bonus Game 2 - Mario Kart 8 Deluxe' -- what appears on the schedule/index
            #   'display_name': 'Mario Kart 8 Deluxe' -- actual game name
            #   'twitch_name': 'Mario Kart 8' -- what the game will be set to on Twitch, often missing
            gamename = run_data[config['run_name_display']]
            category = run_data['category']

            # get runner names and their twitches linked for the final embed
            runners = []  # not a one liner bc it makes them linked
            runners_linked = []
            for rid in run_data['runners']:  # for runner id in list of ids
                data = await self.runner_name(rid)
                runner_name = discord.utils.escape_markdown(data['name'])
                runners.append(runner_name)
                stream_url = data['stream']
                if stream_url:
                    name_temp = "{} {}".format(runner_name, self.social_emoji['twitch']).strip()
                    runner_name = "[{}]({})".format(name_temp, stream_url)
                if data['twitter'] and self.social_emoji['twitter']:
                    runner_name += " [{}](https://twitter.com/{})".format(self.social_emoji['twitter'], data['twitter'])
                if data['youtube'] and data['platform'] != 'YOUTUBE' and self.social_emoji['youtube']:
                    runner_name += " [{}](https://youtube.com/user/{})".format(self.social_emoji['youtube'], data['youtube'])
                runners_linked.append(runner_name)
            human_runners = comma_format(runners)  # -> format with commas
            human_runners_linked = comma_format(runners_linked)

            race_str = " **RACE**" if (not run_data['coop'] and len(runners) > 1) else ""  # says if race or not
            estimate = run_data['run_time']  # run length/estimate

            dtnow = datetime.datetime.now(self.timezone)
            # upcoming games list (channel topic)
            gameslist_prefix = None
            # if one of the upcoming runs:
            if 0 < len(self.gameslist) < config['upcoming_runs']+1:
                htime = humanize.naturaltime(starts_at.astimezone(local_timezone).replace(tzinfo=None))
                gameslist_prefix = htime[0].upper() + htime[1:]  # capitalize first letter
            # if current run:
            elif starts_at <= dtnow < isoparse(run_data['endtime']).astimezone(self.timezone):
                prefix += "\N{BLACK RIGHTWARDS ARROW} "
                gameslist_prefix = "Current Game"
            # if one of the above two if statements executed
            if gameslist_prefix:
                runline = f"{gameslist_prefix}: {gamename} ({category}) by "
                self.gameslist.append(runline + human_runners)
                self.embedlist.append(runline + human_runners_linked)

            output = [f"{prefix}{starts_at_frmt}: {gamename} ({category}){race_str} by {human_runners} in {estimate}"]

            if run_data_base['pk'] in biddex:
                for bid_data in biddex[run_data_base['pk']]:
                    isClosed = bid_data['state'] == 'CLOSED'
                    bidname = bid_data['name']
                    moneyraised = float(bid_data['total'])
                    if bid_data['goal'] is not None:
                        moneygoal = float(bid_data['goal'])
                        emoji = '✅' if moneyraised >= moneygoal else '❌' if isClosed else '⚠️'
                        extradata = f"(${moneyraised:,.2f}/${moneygoal:,.2f}, {int((moneyraised / moneygoal) * 100)}%)"
                    else:
                        emoji = '💰' if isClosed else '⏰'
                        # TODO: haven't currently found a way to get bid war options but they must be somewhere...
                        # extradata = f"(${moneyraised:,.2f})"
                        extradata = f"(<{bid_data['canonical_url']}>)"
                    output.append(f"{emoji} {bidname} {extradata}")

            # gets VOD links from VODThread
            # TODO: look for a new source or do it manually? sometimes misses events (had none for CRDQ)
            # TODO: add YT VODs from VODThread
            if len(gdqvods) - 1 >= runcount:
                vodindex = gdqvods[runcount]
                while vodindex:
                    output.append(f"<https://twitch.tv/videos/{vodindex[0]}?t={vodindex[1]}>")
                    vodindex = vodindex[2:]  # gets next link if there is another
            if len(gdqytvods) - 1 >= runcount:
                vodindex = gdqytvods[runcount]
                if isinstance(vodindex, str):
                    vodindex = [vodindex]
                for vod in vodindex:
                    if vod:  # can be blank strings from un-uploaded runs
                        output.append(f"<https://youtu.be/{vod}>")
            schedule_list.append('\n'.join(output))
            runcount += 1

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
            if outputmsg:
                s_name = "{} {}".format(self.social_emoji['twitch'], config['twitch_channel']).strip()
                desc = [f"Bot created by {self.author}",
                        f"Updates every {config['wait_minutes']} minutes",
                        f"Watch live at [{s_name}](https://twitch.tv/{config['twitch_channel']})"]
                embed = discord.Embed(title=f"{self.event.upper()} Run Roster",
                                      description='\n'.join(desc),
                                      timestamp=datetime.datetime.utcnow(), color=0x3bb830)
                for run in outputmsg:
                    # from the self.embedlist, the messages take the format of "Current Run: Game (Category) by Runners"
                    run_when = run.split(':')[0].strip()
                    run_desc = ':'.join(run.split(':')[1:]).strip()
                    embed.add_field(name=run_when, value=run_desc, inline=False)
                    embed.set_footer(text="Last updated:")
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

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

    async def on_message(self, message):
        if message.channel.id == 460520708414504961 and message.type == discord.MessageType.pins_add:
            await message.delete()

    async def my_background_task(self):
        await self.wait_until_ready()
        # load emojis
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
        # load session
        global session
        session = aiohttp.ClientSession()
        # load event info
        index = await load_gdq_index()
        self.event = index['short']
        self.timezone = pytz.timezone(index['timezone'])
        for runner_raw_data in (await load_gdq_json(f"?type=runner&event={config['event_id']}")):
            self.runners[runner_raw_data['pk']] = runner_raw_data['fields']
        # get channel
        rushschd = self.get_channel(config['schedule_channel'])
        assert rushschd is not None
        init = True

        # Start background loop
        while not self.is_closed():
            # avoid an redundant API call
            if not init:
                index = await load_gdq_index()
            else:
                init = False

            # donation ping% / status changer
            donations = float(index['amount'])
            donomsg = f"${donations:,.2f} donations"
            activ = discord.Activity(type=discord.ActivityType.watching, name=donomsg)
            await client.change_presence(activity=activ)

            try:  # the SCHEDULE
                # reset variables
                self.gameslist = []
                self.embedlist = []
                self.msgIndex = 0
                # get schedule
                schedule = await self.human_schedule()
                schedule.append(self.embedlist)  # add data for embed
                dtoffset = datetime.datetime.utcnow() - datetime.timedelta(days=10)
                # update/post the schedule messages
                async for message in rushschd.history(after=dtoffset, limit=None):
                    if message.author == self.user:
                        await self.process_message(schedule, message=message)
                while self.msgIndex < len(schedule):
                    await self.process_message(schedule, channel=rushschd)
                print(f"{datetime.datetime.now()} Schedule Updated!")
            except Exception as e:
                print(f"SCHEDULE: {e}")
                traceback.print_exc()

            await rushschd.edit(topic='\n\n'.join(self.gameslist))

            await asyncio.sleep(60 * config['wait_minutes'])


client = DiscordClient(allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
client.run(config['token'], bot=True)
