import asyncio
import re
import typing

import aiohttp
import discord
from yaml import load

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


class DiscordClient(discord.Client):
    # request headers
    gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}
    murphy_ping = re.compile(r"<@!?(?:187684157181132800|460906275400843274)>")
    channel_id = 442082610785550337
    amount_regex = re.compile(r"^\$?((?:\d{1,3},?){1,3}(?:\.\d+)?)(?: ?([MKmk]))?")
    suffix_map = {
        'm': 1000000,
        'k': 1000
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = load(open('config.yaml', 'r'), Loader)
        # aiohttp session, do not change
        # (it gets defined later because it yelled at me for creating in non-async func)
        self.session: typing.Optional[aiohttp.ClientSession] = None

    async def load_gdq_json(self, query):
        """
        Loads and processes a GDQ API page
        :param query: the search parameters to query
        :return: json object
        """
        url = f"{self.config['gdq_url']}{query}"
        async with self.session.get(url, **self.gdq_headers) as r:
            if r.status == 200:
                jsondata = await r.json()
            else:
                print("GET {} returned {} {} -- aborting".format(url, r.status, await r.text()))
                exit()
        return jsondata

    async def load_gdq_index(self):
        """
        Returns the GDQ index (main) page, includes donation totals
        :return: json object
        """
        return (await self.load_gdq_json(f"?type=event&id={self.config['event_id']}"))[0]['fields']

    async def load_donation_total(self) -> float:
        """
        Returns the current GDQ donation total
        :return: float
        """
        return float((await self.load_gdq_index())['amount'])

    async def on_ready(self):
        self.session = aiohttp.ClientSession()
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

        # load event info
        if not isinstance(self.config['event_id'], int):
            orig_id = self.config['event_id'].lower()
            events = await self.load_gdq_json(f"?type=event")
            self.config['event_id'] = next((event['pk'] for event in events if event['fields']['short'].lower() == orig_id), None)
            if self.config['event_id'] is None:
                print(f"Could not find event {orig_id}")
                exit()

    async def handle(self, msg: discord.Message):
        if self.session is None:
            return
        if msg.channel.id != self.channel_id:
            return
        content = self.murphy_ping.sub("", msg.content).strip()
        if content == msg.content or len(content) == 0:
            return
        match = self.amount_regex.match(content)
        if not match:
            return
        amount = float(match.group(1).replace(',', ''))
        if match.group(2):
            amount *= self.suffix_map[match.group(2).lower()]

        current_amount = await self.load_donation_total()
        # conversion to int gives users benefit of the doubt in regard to rounding errors
        if int(current_amount) >= int(amount):
            return

        await msg.reply(f"liar! >:( we're at only ${current_amount:,.2f}, not ${amount:,.2f}.")

    async def on_message(self, msg: discord.Message):
        await handle(msg)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        await handle(after)


if __name__ == '__main__':
    intents = discord.Intents.default()
    intents.message_content = True
    client = DiscordClient(intents=intents, allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
    client.run(client.config['token'])
