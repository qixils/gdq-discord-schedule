import asyncio
import json
from statistics import mean
from operator import sub
import traceback

import aiohttp
import discord
from discord.ext import tasks
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


config = load(open('config.yaml', 'r'), Loader)

# Murphy's Ping% Game: every [x] donation amount, Murphy will be pinged.
# set this variable to None to disable
# TODO: copy from raspi
murph_donations = list(range(5000, 50000, 5000)) + list(range(50000, 100000, 10000)) + list(range(100000, 300000, 20000)) + list(range(300000, 700000, 10000)) + list(range(700000, 1000000, 20000)) + list(range(1000000, 1800000, 25000)) + list(range(1800000, 10000000, 50000))
# channel ID for murphy's game
murph_channel_id = 442082610785550337
murph = 187684157181132800
# donation prediction game file
predictions = json.load(open('predictions.json', 'r'))  # todo: create new predictions file

# aiohttp session, do not change
session: aiohttp.ClientSession = None  # gets defined later because it yelled at me for creating in non-async func

# request headers
gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}

run_every = 10.0
all_donation_length = 6 + 1


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


def comma_format(input_list):
    *a, b = input_list
    return ' and '.join([', '.join(a), b]) if a else b


class GDQGames(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel = None
        self.donation_milestones = []  # ping% milestones that have been reached
        self.first_donation_check = True  # if this is the ping% init
        self.tie_lock = asyncio.Lock()  # prevents race conditions
        self.tie_tracker = {}  # dict of datetime's to track ties in ping%
        self.lost = []  # users whose predictions have lost

        self.donations = 0
        self.all_donations = []
        self.prefix = 't!'

        self.gamer.start()  # start game loop

    async def on_ready(self):
        print('Logged in as {0!s} ({0.id})'.format(self.user))
        print('---')

    async def on_message(self, message: discord.Message):
        if message.content.startswith(self.prefix):
            cmd = message.content.replace(self.prefix, '', 1)
            if cmd in ['donations', 'totals', 'total', 'amount', 'raised']:
                await message.channel.send(f"${self.donations:,.2f}")
            elif cmd in ['rate']:
                if len(self.all_donations) <= 1:
                    await message.channel.send(f"Not enough data yet, please wait")
                else:
                    rate = list(map(sub, self.all_donations[1:], self.all_donations[:-1]))
                    await message.channel.send(f"Average donation rate per {int(run_every)} seconds over the past {int(run_every*len(rate))} seconds: ${mean(rate):,.2f}")
        if message.mentions:
            if discord.utils.get(message.mentions, id=murph):
                authid = message.author.id
                created = message.created_at
                async with self.tie_lock:
                    if created not in self.tie_tracker:
                        self.tie_tracker[created] = [authid]
                    else:
                        users = list(map(self.get_user, self.tie_tracker[created] + [authid]))
                        await self.channel.send("{} tied in Ping%!".format(comma_format(users)))
                    self.tie_tracker[created].append(authid)

    @tasks.loop(seconds=run_every)
    async def gamer(self):
        try:
            index = await load_gdq_index()
            self.donations = float(index['amount'])
            self.all_donations.append(self.donations)
            while len(self.all_donations) > all_donation_length:
                del self.all_donations[0]

            for x in murph_donations:
                if self.donations >= x and x not in self.donation_milestones:
                    if not self.first_donation_check:
                        totals = list(map(int, f"{x:,}".split(',')))
                        if len(totals) == 2:
                            y = f"{totals[0]}K"
                        elif len(totals) == 3:
                            decimal = f".{totals[1]:03}"
                            while decimal.endswith('0') or decimal.endswith('.'):
                                decimal = decimal[:-1]
                            y = f"{totals[0]}{decimal}M"
                        else:  # weird edge case?? use legacy message
                            y = f"${x:,}"
                        out = f"<@{murph}> {y}"
                        mentions = discord.AllowedMentions(users=[self.get_user(murph)])
                        await self.channel.send(out, allowed_mentions=mentions)
                    self.donation_milestones.append(x)

            loser = ""
            winner = ""
            users = []
            for prediction in predictions:
                if prediction['ping'] not in self.lost:
                    if self.donations > prediction['max']:
                        self.lost.append(prediction['ping'])
                        if not loser:
                            user = discord.Object(prediction['ping'])
                            users.append(user)
                            loser = "<@{}>'s donation total prediction of ${:,.2f} has been surpassed.".format(
                                prediction['ping'], prediction['amount'])
                    elif loser and not winner:  # i don't *need* the 'if loser' part buut it feels safer
                        user = self.get_user(prediction['ping'])
                        users.append(user)
                        winner = "The next closest prediction is {}'s guess of ${:,.2f}.".format(user.mention,
                                                                                                 prediction['amount'])
            if not self.first_donation_check and loser and winner:
                allowed = discord.AllowedMentions(users=users)
                await self.channel.send(f"{loser}\n{winner}", allowed_mentions=allowed)
            self.first_donation_check = False
        except:
            traceback.print_exc()

    @gamer.before_loop
    async def before_gamer(self):
        global session
        session = aiohttp.ClientSession()

        if not isinstance(config['event_id'], int):
            orig_id = config['event_id'].lower()
            events = await load_gdq_json(f"?type=event")
            config['event_id'] = next((event['pk'] for event in events if event['fields']['short'].lower() == orig_id), None)
            if config['event_id'] is None:
                print(f"Could not find event {orig_id}")
                exit()

        await self.wait_until_ready()
        self.channel = self.get_channel(murph_channel_id)


client = GDQGames(allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
client.run(config['token'], bot=True)
