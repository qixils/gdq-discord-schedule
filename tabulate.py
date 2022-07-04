import logging
import sys
import typing
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


logger = logging.getLogger("tabulate")


def process(filename: str) -> typing.Dict[str, int]:
    with open(filename, 'r') as f:
        data = load(f, Loader)
    scores = {}
    for di, day_data in enumerate(data):
        rewards = day_data['rewards']
        for wi, winners in enumerate(day_data['winners']):
            if len(winners) < len(rewards):
                logger.warning(f"Entry {wi+1} of day {di+1} has fewer winners than rewards")
            for i, winner in enumerate(winners):
                if winner not in scores:
                    scores[winner] = 0
                if i < len(rewards):
                    scores[winner] += rewards[i]
                else:
                    logger.warning(f"{winner} (#{i+1}) is marked as a winner"
                                   f" but has no reward ({rewards})")
    return scores


def main():
    args = sys.argv[1:]
    if len(args) > 1:
        logger.error("Usage: tabulate.py [filename]")
        sys.exit(1)
    elif len(args) == 1:
        filename = args[0]
    else:
        filename = input("Please enter the filename to process: ")

    scores = sorted(process(filename).items(), key=lambda x: x[1], reverse=True)
    for i, (name, score) in enumerate(scores, start=1):
        print(f"{i:>2}. {name} ({score})")


if __name__ == '__main__':
    main()
