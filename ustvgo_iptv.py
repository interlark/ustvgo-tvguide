import asyncio
import json
import logging
import pathlib
from functools import partial

from tqdm.asyncio import tqdm


USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/102.0.5005.63 Safari/537.36')
USTVGO_HEADERS = {'Referer': 'https://ustvgo.tv/', 'User-Agent': USER_AGENT}

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s :: %(levelname)s :: %(message)s',
    datefmt='%H:%M:%S'
)

logger = logging.getLogger(__name__)


def root_dir():
    """Root directory."""
    return pathlib.Path(__file__).parent


def load_dict(filename):
    """Load root dictionary."""
    filepath = root_dir() / filename
    with open(filepath, encoding='utf-8') as f:
        return json.load(f)


async def gather_with_concurrency(n, *tasks, show_progress=True, progress_title=None):
    """Gather tasks with concurrency."""
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task

    gather = partial(tqdm.gather, desc=progress_title) if show_progress \
        else asyncio.gather
    return await gather(*[sem_task(x) for x in tasks])
