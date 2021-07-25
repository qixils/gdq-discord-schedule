import asyncio
import datetime
from datetime import datetime as dtlib
import json
import math
import re
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
from discord.ext import tasks


config = load(open('config.yaml', 'r'), Loader)

# local timezone for appropriately displaying when the upcoming run is
local_timezone = pytz.timezone(config['local_timezone'])

# request headers
gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}
reddit_headers = {"headers": {"User-Agent": "simple-wiki-reader:v0.1 (/u/noellekiq)"}}  # add your own reddit username here?

# aiohttp session, do not change
session: aiohttp.ClientSession = None  # gets defined later because it yelled at me for creating in non-async func

fix_space: re.Pattern = re.compile(" +")


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

            # GDQ doesn't provide official ratelimits, so we apply our own safe amount
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
    wiki_page = wiki_page.lower()
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
def comma_format(input_list) -> str:
    if not input_list:
        return ''
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


def bkup_link(_dir: str, _id: str):
    _dir, _id = str(_dir), str(_id)
    bkup_lnk_raw = config['gdq_url'].split('/')
    bkup_lnk_raw[-1] = _id
    bkup_lnk_raw[-2] = _dir
    return '/'.join(bkup_lnk_raw)


utc = pytz.timezone('UTC')
_1970 = dtlib(1970, 1, 1)


def timestamp_obj_of(dt: datetime.datetime, mode: str = "") -> str:
    return f"<t:{math.floor((dt.astimezone(utc).replace(tzinfo=None) - _1970).total_seconds())}:{mode}>"


class DiscordClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.author = "qixils#0493"  # me, the bot creator :)

        self.social_emoji = {}  # emojis used for social media links
        self.runners = {}  # dict of runner_id: fields

        # start the background schedule processor
        self.processor.start()

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

    async def on_message(self, message):
        if message.channel.id in config['schedule_channel'] and message.type == discord.MessageType.pins_add and \
                message.channel.permissions_for(message.guild.me).manage_messages:
            await message.delete()

    async def human_schedule(self):
        """
        Processes the human-readable schedule.
        :return: list of runs
        """
        # load pages
        schedule = await load_gdq_json(f"?type=run&event={config['event_id']}")
        gdqvods = await load_json_from_reddit(f'{self.event}vods')
        gdqytvods = await load_json_from_reddit(f'{self.event}yt', log_errors=False)
        bids = await load_gdq_json(f"?type=bid&event={config['event_id']}")
        bidoptions = await load_gdq_json(f"?type=bidtarget&event={config['event_id']}")

        # Header Message
        index = await load_gdq_index()
        dnmsg1 = "Join the {dns} donators who have raised {amt} for {cha} at {lnk}. (Minimum Donation: {mnd})"
        dnmsg2 = "Raised {amt} from {dns} donators for {cha}. "
        dnmsg = dnmsg1 if not index['locked'] else dnmsg2
        lnk = index['canonical_url'] if 'canonical_url' in index else bkup_link("index", index['short'])
        dnmsg = dnmsg.format(dns=f"{int(index['count']):,}", amt=f"${float(index['amount']):,.2f}",
                             cha=index['receivername'], lnk=lnk,
                             mnd=f"${float(index['minimumdonation']):,.2f}")
        outputmsg = '\n'.join([f"**{index['name']}**",
                               f"All times are in {self.timezone}.",
                               dnmsg])
        schedule_list = [outputmsg]

        current_date = datetime.date(year=1970, month=1, day=15)  # for splitting schedule by end of day

        # create index of bids, {run_id: [bid1, bid2, ...]}, for efficient bid iteration
        biddex = {}  # portmanteau of bid index, ha!
        for bidorigin in bids:
            bidorigrunid = bidorigin['fields']['speedrun']
            if bidorigrunid not in biddex:
                biddex[bidorigrunid] = []
            biddex[bidorigrunid].append(bidorigin)

        # create index of bid options
        optiondex = {}
        for optorigin in bidoptions:
            parentid = optorigin['fields']['parent']
            if parentid not in optiondex:
                optiondex[parentid] = []
            optiondex[parentid].append(optorigin['fields'])

        # finally iterate through every run
        for runcount, run_data_base in enumerate(schedule):
            run_data = run_data_base['fields']  # all run data contained in here (except the ID)

            starts_at = isoparse(run_data['starttime']).astimezone(self.timezone)  # converts utc time to event time
            _starts_at_frmt = timestamp_obj_of(starts_at, 'd')
            starts_at_frmt = _starts_at_frmt + " " + _starts_at_frmt.replace('d', 't')
            # adds the new day separator
            prefix = ''
            if starts_at.date() > current_date:
                prefix += fix_space.sub(" ", starts_at.strftime("_ _%n> **%A** %b %e%n_ _%n"))
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
                data = self.runners[rid]
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
            if runners:
                human_runners = comma_format(runners)  # -> format with commas
                human_runners_linked = comma_format(runners_linked)
            else:
                human_runners = human_runners_linked = "[nobody]"

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
                    bid_id = bid_data['pk']
                    bid_data = bid_data['fields']
                    is_closed = bid_data['state'] == 'CLOSED'
                    bidname = bid_data['name']
                    moneyraised = float(bid_data['total'])
                    if bid_data['goal'] is not None:
                        moneygoal = float(bid_data['goal'])
                        # TODO: replace emoji chars with \N{} or something
                        if moneyraised >= moneygoal:
                            emoji = '✅'
                        elif is_closed:
                            emoji = '❌'
                        else:
                            emoji = '⚠️'
                        extradata = f"${moneyraised:,.2f}/${moneygoal:,.2f}, {int((moneyraised / moneygoal) * 100)}%"
                    else:
                        emoji = '💰' if is_closed else '⏰'
                        if bid_id in optiondex and optiondex[bid_id]:
                            optfields = optiondex[bid_id]
                            templist = [o2['name'] for o2 in sorted(optfields, reverse=True, key=lambda o1: float(o1['total']))[:3]]
                            if len(optfields) > 3:
                                templist.append('...')
                            templist[0] = f"**{templist[0]}**"
                            extradata = '/'.join(templist)
                        else:
                            bid_lnk = bid_data['canonical_url'] if 'canonical_url' in bid_data else bkup_link("bid",
                                                                                                              bid_id)
                            extradata = f"<{bid_lnk}>"
                    output.append(f"{emoji} {bidname} ({extradata})")

            # gets VOD links from VODThread
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
            s_name = "{} {}".format(self.social_emoji['twitch'], config['twitch_channel']).strip()
            desc = [f"Bot created by {self.author}",
                    f"Updates every {config['wait_minutes']} minutes",
                    f"Watch live at [{s_name}](https://twitch.tv/{config['twitch_channel']})"]
            if self.event.lower().startswith('esa'):
                desc.append("")
                desc.append("__**ESA is notoriously bad at updating their schedules. "
                            "Take these times and estimates with a grain of salt.**__")
            embed = discord.Embed(title=f"{self.eventname} Run Roster",
                                  description='\n'.join(desc),
                                  timestamp=datetime.datetime.utcnow(), color=0x3bb830)
            embed.set_footer(text="Last updated:")
            if outputmsg:
                for run in outputmsg:
                    # from the self.embedlist, the messages take the format of "Current Run: Game (Category) by Runners"
                    run_when = run.split(':')[0].strip()
                    run_desc = ':'.join(run.split(':')[1:]).strip()
                    embed.add_field(name=run_when, value=run_desc, inline=False)
            else:
                val = "The event has ended. Thank you all for watching and donating!" if datetime.datetime.utcnow().astimezone(self.timezone) > self.starttime \
                    else self.starttime.strftime("The event will start on %A %b %e.")
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
        index = await load_gdq_index()
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
            dtoffset = self.starttime.astimezone(pytz.timezone('UTC')).replace(tzinfo=None) - datetime.timedelta(days=1)
            # update/post the schedule messages
            index_copy = self.msgIndex  # lazy fix for multi channels
            for chan in self.channels:
                self.msgIndex = index_copy
                async for message in chan.history(after=dtoffset, limit=None):
                    if message.author == self.user:
                        await self.process_message(schedule, message=message)
                while self.msgIndex < len(schedule):
                    await self.process_message(schedule, channel=chan)
                print(f"[{datetime.datetime.now()}] #{chan}: Schedule Updated!")
        except Exception as e:
            print(f"SCHEDULE: {e}")
            traceback.print_exc()

        for chan in self.channels:
            await chan.edit(topic='\n\n'.join(self.gameslist))

    @processor.before_loop
    async def before_processor(self):
        # load session
        global session
        session = aiohttp.ClientSession()

        # load event info
        if not isinstance(config['event_id'], int):
            orig_id = config['event_id'].lower()
            events = await load_gdq_json(f"?type=event")
            config['event_id'] = next((event['pk'] for event in events if event['fields']['short'].lower() == orig_id), None)
            if config['event_id'] is None:
                print(f"Could not find event {orig_id}")
                exit()
        index = await load_gdq_index()
        self.event = index['short']
        self.eventname = index['name']
        self.timezone = pytz.timezone(index['timezone'])
        if 'datetime' in index:
            dt_str = index['datetime']
        else:
            dt_str = index['date']
        self.starttime = isoparse(dt_str).astimezone(self.timezone)
        for runner_raw_data in (await load_gdq_json(f"?type=runner&event={config['event_id']}")):
            self.runners[runner_raw_data['pk']] = runner_raw_data['fields']

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
        self.channels = list(filter(lambda x: x is not None, map(lambda x: self.get_channel(x), config['schedule_channel'])))
        assert len(self.channels) == len(config['schedule_channel'])


client = DiscordClient(allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
client.run(config['token'], bot=True)
