import re
import typing
from datetime import datetime
import os
import requests
import time

from yaml import load

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


class Watcher:
    # request headers
    gdq_headers = {"headers": {"User-Agent": "rush-schedule-updater"}}
    last_total = 0
    target_modulo = 25000
    fast_tick = 1000
    super_fast_tick = 100
    hit_target_at = None

    def __init__(self):
        self.config = load(open('config.yaml', 'r'), Loader)
        # load event info
        if not isinstance(self.config['event_id'], int):
            orig_id = self.config['event_id'].lower()
            events = self.load_gdq_json(f"?type=event")
            self.config['event_id'] = next((event['pk'] for event in events if event['fields']['short'].lower() == orig_id), None)
            if self.config['event_id'] is None:
                print(f"Could not find event {orig_id}")
                exit()
        while True:
            self.processor()

    def load_gdq_json(self, query):
        """
        Loads and processes a GDQ API page
        :param query: the search parameters to query
        :return: json object
        """
        url = f"{self.config['gdq_url']}{query}"
        with requests.get(url, **self.gdq_headers) as r:
            if r.status_code == 200:
                jsondata = r.json()
            else:
                print("GET {} returned {} {} -- aborting".format(url, r.status_code, r.text))
                exit()
        return jsondata

    def load_gdq_index(self):
        """
        Returns the GDQ index (main) page, includes donation totals
        :return: json object
        """
        return self.load_gdq_json(f"?type=event&id={self.config['event_id']}")[0]['fields']

    def load_donation_total(self) -> float:
        """
        Returns the current GDQ donation total
        :return: float
        """
        return float(self.load_gdq_index()['amount'])

    def get_prev_target(self, total) -> float:
        return total - (total % self.target_modulo)

    def get_next_target(self, total) -> float:
        return self.get_prev_target(total) + self.target_modulo

    def processor(self):
        total = self.load_donation_total()
        target = self.get_next_target(total)
        prev_target = self.get_prev_target(total)
        if self.last_total > 0 and self.get_next_target(self.last_total) != target:
            self.hit_target_at = datetime.now()

        os.system('clear')
        print(f"Most Recent Target: ${prev_target:,.0f}")
        print(f"Reached At:         {self.hit_target_at or 'N/A'}")
        diff_since_prev = total - prev_target
        print(f"$ Since Target:     ${diff_since_prev:,.2f}")
        print("")
        print(f"Current Total:  ${total:,.2f}")
        print(f"Next Target:    ${target:,.0f}")
        diff_until_next = target - total
        print(f"$ Until Target: ${diff_until_next:,.2f}")
        print("")
        print(f"Last updated at {datetime.now()}")

        prev_diff_until_next = self.get_next_target(self.last_total) - self.last_total
        if prev_diff_until_next > self.super_fast_tick and diff_until_next <= self.super_fast_tick:
            os.system('notify-send "A donation target is approaching! (<$100)" --urgency=critical --app-name="GDQ Watcher" --icon=data-warning')
        elif prev_diff_until_next > self.fast_tick and diff_until_next <= self.fast_tick:
            os.system('notify-send "A donation target is approaching! (<$1000)" --urgency=normal --expire-time=20000 --app-name="GDQ Watcher" --icon=data-information')

        self.last_total = total
        if diff_until_next <= self.super_fast_tick:
            time.sleep(0.5)
        elif diff_until_next <= self.fast_tick:
            time.sleep(1)
        else:
            time.sleep(10)


if __name__ == '__main__':
    client = Watcher()
