#!/usr/bin/env python3

import argparse
import asyncio
import atexit
import functools
import gzip
import io
import json
import pathlib
import shutil
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial

import aiohttp
import xmltv.models
from diskcache import Cache
from furl import furl
from PIL import Image
from pydantic import ValidationError
from tqdm import tqdm
from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

import models.tvguide
import models.ustvgo
from ustvgo_iptv import (USER_AGENT, USTVGO_HEADERS, gather_with_concurrency,
                         load_dict, logger, root_dir)

# Usage:
# ./epg-downloader.py ustvgo.xml --create-archive


VERSION = '0.1.1'
DISK_CACHE = Cache(root_dir() / 'cache', size_limit=2**32)  # 2**32 bytes == 4 GB
DISK_CACHE_EXPIRE = int(timedelta(days=3).total_seconds())  # 3 days cache expire

XMLTV_PROGRAM_OPTIONS = {
    # Whether to expand genres
    'expand_genres': True,

    # Whether to add TV-Rating icons to XMLTV (some EPG consumers don't support it)
    'add_tv_rating_icon': False
}


@atexit.register
def close_cache():
    """Cleanup cache and close."""
    DISK_CACHE.expire()
    DISK_CACHE.close()


def download_cached_by_url(func):
    """Cached wrapper for `download_with_retries`."""
    @functools.wraps(func)
    async def inner(url, *args, **kwargs):
        result = DISK_CACHE.get(key=url)
        if not result:
            result = await func(url, *args, **kwargs)
            if result:
                DISK_CACHE.set(key=url, value=result, expire=DISK_CACHE_EXPIRE)

        return result

    return inner


@download_cached_by_url
async def download_with_retries(url, headers=None, timeout=1, timeout_increment=1,
                                timeout_max=10, retries_max=10, method='json',
                                extra_exceptions=None, loader=None, ret_default=None):
    """Download URL with retries."""
    exceptions = [asyncio.TimeoutError, aiohttp.ClientConnectionError,
                  aiohttp.ClientResponseError, aiohttp.ServerDisconnectedError]
    if method == 'json':
        exceptions.append(json.JSONDecodeError)
    if extra_exceptions:
        exceptions.extend(extra_exceptions)

    loader = loader if loader else lambda x: x
    retry = 1
    while True:
        try:
            async with aiohttp.ClientSession(headers=headers, raise_for_status=True) as session:
                async with session.get(url, timeout=timeout) as response:
                    return loader(await getattr(response, method)())
        except Exception as e:
            is_exc_valid = any([isinstance(e, exc) for exc in exceptions])
            if not is_exc_valid:
                raise
            timeout = min(timeout + timeout_increment, timeout_max)
            if retry > retries_max:
                logger.error('Failed to download URL %s', url)
                return ret_default
            retry += 1


async def download_programs(channel):
    """Download list of upcoming programs from USTVGO endpoint."""
    if not channel['tvguide_id']:
        channel['programs'] = []
        return

    url = 'https://ustvgo.tv/tvguide/JSON2/%s.json?_=%d' \
        % (channel['tvguide_id'], time.time())  # NOTE: 100% cache miss

    def loader(response):
        programs = sum(response.get('items', {}).values(), [])
        return [models.ustvgo.Program(**program) for program in programs]

    channel['programs'] = await download_with_retries(
        url, USTVGO_HEADERS, loader=loader, ret_default=[],
        extra_exceptions=[ValidationError, AttributeError],
    )


async def download_program_detail(program):
    """Download program details from tvguide.com"""
    headers = {'Referer': 'https://google.com', 'User-Agent': USER_AGENT}
    url = ('https://cmg-prod.apigee.net/v1/xapi/tvschedules/'
           'tvguide/programdetails/%d/web' % program.id)

    def loader(response):
        return models.tvguide.ProgramDetails(**response['data']['item'])

    program._details = await download_with_retries(
        url, headers, loader=loader,
        extra_exceptions=[ValidationError, KeyError]
    )


async def download_program_cast(program):
    """Download program Cast & Crew."""
    if program._details and program._details.mcoId:
        headers = {'Referer': 'https://google.com', 'User-Agent': USER_AGENT}
        url = ('https://cmg-prod.apigee.net/v1/xapi/composer/tvguide/pages/'
               'shows-cast/%d/web?contentOnly=true' % program._details.mcoId)

        def loader(response):
            # Find "Cast & Crew" component
            for component in response.get('components', []):
                meta = component.get('meta', {})
                if meta.get('componentName') == 'tv-object-cast-and-crew':
                    cast_data = component.get('data', {})
                    if cast_data:
                        return models.tvguide.ShowsCast(**cast_data)

            # Component not found, return dummy cast
            return models.tvguide.ShowsCast(id='0', items=[])

        program._cast = await download_with_retries(
            url, headers, loader=loader,
            extra_exceptions=[ValidationError, KeyError, AttributeError]
        )


async def download_program_images(program, images_size, images_quality, base_url):
    """Download and resize program images."""
    if not program._details:
        return  # Nothing to download, bail

    def loader(response):
        """Image resize in loader for
        reducing disk cache size."""
        with Image.open(io.BytesIO(response)) as img:
            img.thumbnail((images_size, images_size))
            bytesio = io.BytesIO()
            img.save(bytesio, format=img.format, quality=images_quality)
            return bytesio.getbuffer().tobytes()

    for image in program._details.images:
        try:
            # Download image
            img_bytes = await download_with_retries(
                image.url, method='read', loader=loader,
                timeout=15, timeout_max=120, timeout_increment=10
            )

            # Path for img
            img_path = root_dir() / 'images' / 'posters' / image.bucketPath.lstrip('/')
            img_path.parent.mkdir(parents=True, exist_ok=True)

            # Save preloaded image
            iobytes = io.BytesIO(img_bytes)
            img_path.write_bytes(iobytes.getbuffer())

            # Update image parameters
            with Image.open(iobytes) as img:
                image.width = img.width
                image.height = img.height
                image.bucketPath = (furl(base_url) / 'images/posters' / image.bucketPath).url
                image.bucketType = 'local'
        except Exception as e:
            logger.warn(('Something bad happened during '
                         'working with image: %s (URL: %s)', e, image.url))


async def download_program_tags(channels):
    """Download tags for programs."""
    start_date = datetime.utcnow() - timedelta(minutes=30)
    start_ts = int(start_date.timestamp())
    duration_mins = 60 * 12
    provider_id = '9100001138'  # Eastern Time Zone
    url = ('https://cmg-prod.apigee.net/v1/xapi/tvschedules'
           f'/tvguide/{provider_id}/web?start={start_ts}&duration={duration_mins}')
    headers = {**USTVGO_HEADERS, 'Referer': 'https://www.tvguide.com/'}

    def loader(response):
        programs = sum([x['programSchedules'] for x in response['data']['items']], [])
        programs_and_attrs = {x['programId']: x['airingAttrib'] for x in programs
                              if x['airingAttrib'] and x['programId']}
        return programs_and_attrs

    data = await download_with_retries(url, headers, loader=loader)
    programs_new = {k for k, v in data.items() if v & 0b100}
    programs_live = {k for k, v in data.items() if v & 0b1}

    for channel in channels:
        for program in channel['programs']:
            if program.id in programs_new:
                program.tags.append('new')

            if program.id in programs_live:
                program.tags.append('live')


@lru_cache
def icon_manifest(manifest_name):
    """Load icon manifest."""
    manifest_path = root_dir() / 'images' / 'icons' / f'{manifest_name}.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    return manifest


def xmltv_icon(icon_name, manifest_name, base_url):
    """Get XMLTV icon."""
    manifest = icon_manifest(manifest_name)
    if icon_name in manifest:
        icon_info = manifest[icon_name]
        icon_src = (furl(base_url) / 'images/icons' / icon_info['path']).url
        return xmltv.models.Icon(src=icon_src, width=icon_info['width'], height=icon_info['height'])

    return None


def make_xmltv(channels, filepath, base_url, icons_for_light_bg):
    """Make XMLTV document out of stored channels and collected programs."""
    tv = xmltv.models.Tv(
        generator_info_name='ustvgo-iptv',
        generator_info_url='https://github.com/interlark/ustvgo-iptv',
        date=datetime.now().strftime('%Y%m%d%H%M%S')
    )

    get_icon = partial(xmltv_icon, base_url=base_url)

    for channel in tqdm(channels, desc='Make EPG XMLTV'):
        # Add channels
        channel_id = channel['stream_id']
        xmltv_channel = xmltv.models.Channel(
            display_name=channel['name'],
            id=channel_id
        )

        channels_manifest_name = 'channels'
        if icons_for_light_bg:
            channels_manifest_name += '-for-light-bg'
        else:
            channels_manifest_name += '-for-dark-bg'

        channel_icon = get_icon(channel_id, channels_manifest_name)
        if channel_icon:
            xmltv_channel.icon.append(channel_icon)
        else:
            logger.warning(f'Failed to get channel icon "{channel_id}"')

        tv.channel.append(xmltv_channel)

        # Add programs
        for program in channel['programs']:
            if program._details:
                # Convert program details to xmltv program
                xmltv_program = program._details.to_xmltv(
                    get_icon=get_icon, lang=channel['language'],
                    **XMLTV_PROGRAM_OPTIONS
                )
            else:
                # Create program without details
                xmltv_program = xmltv.models.Programme(
                    title=[xmltv.models.Title(content=[program.name])],
                    clumpidx=None,
                )

            # Bind current channel to the program
            xmltv_program.channel = channel['stream_id']

            # Add tags
            if 'new' in program.tags:
                xmltv_program.new = ''

            # Start / End dates
            start_ts = datetime.fromtimestamp(program.start_timestamp, tz=timezone.utc)
            end_ts = datetime.fromtimestamp(program.end_timestamp, tz=timezone.utc)

            xmltv_program.start = start_ts.strftime('%Y%m%d%H%M%S %z')
            xmltv_program.stop = end_ts.strftime('%Y%m%d%H%M%S %z')

            # Add Cast & Crew
            if program._cast:
                program._cast.add_cast(xmltv_program)

            tv.programme.append(xmltv_program)

    # Write EPG XMLTV to target file path
    write_file_from_xml(filepath, tv, base_url)


def write_file_from_xml(xml_filepath, serialize_class, base_url):
    """Method to write serialized XML data to a file."""
    serializer = XmlSerializer(config=SerializerConfig(
        pretty_print=True,
        encoding='UTF-8',
        xml_version='1.0',
        xml_declaration=True,
        schema_location=furl(base_url).add(path='resources/xmltv.xsd').url
    ))

    with xml_filepath.open('w') as data:
        serializer.write(data, serialize_class)


async def download_and_make_epg(filepath, parallel, create_archive, images_size,
                                images_quality, base_url, icons_for_light_bg):
    """Download channels' programs and make XMLTV EPG."""
    channels = load_dict('channels.json')
    download_tasks = [download_programs(channel) for channel in channels]

    # Download programs per each channel from USTVGO
    await gather_with_concurrency(parallel, *download_tasks, progress_title='Download programs')

    # Download program details from TVGUIDE
    for channel in tqdm(channels, desc='Download details'):
        download_tasks = [download_program_detail(program) for program in channel['programs']]
        await gather_with_concurrency(parallel, *download_tasks, show_progress=False)

    # Download program cast (actors, directors, writers, etc) from TVGUIDE
    for channel in tqdm(channels, desc='Download credits'):
        download_tasks = [download_program_cast(program) for program in channel['programs']]
        await gather_with_concurrency(parallel, *download_tasks, show_progress=False)

    # Download and resize images from TVGUIDE
    shutil.rmtree(root_dir() / 'images' / 'posters', ignore_errors=True)  # Remove old imgs first
    for channel in tqdm(channels, desc='Download images'):
        download_tasks = [download_program_images(program, images_size, images_quality, base_url)
                          for program in channel['programs']]
        await gather_with_concurrency(parallel, *download_tasks, show_progress=False)

    # Add tags for programs,
    # could be usefull for IPTV recorders.
    await download_program_tags(channels)

    # Make EPG
    make_xmltv(channels, filepath, base_url, icons_for_light_bg)

    if create_archive:
        with gzip.open(f'{filepath}.gz', 'wb') as f:
            f.write(filepath.read_bytes())


def main():
    parser = argparse.ArgumentParser('epg-downloader')
    parser.add_argument('filepath', type=pathlib.Path)
    parser.add_argument(
        '--parallel', '-p', metavar='N', type=int, default=10,
        help='Number of parallel requests (default: %(default)s)'
    )
    parser.add_argument(
        '--create-archive', '-a', action='store_true',
        help='Create archive of target XML'
    )
    parser.add_argument(
        '--images-size', type=int, metavar='SIZE', default=720,
        help='Set images size (default: %(default)s)'
    )
    parser.add_argument(
        '--images-quality', type=int, metavar='N', default=80,
        help='Set images quality (default: %(default)s)'
    )
    parser.add_argument(
        '--base-url', metavar='URL',
        default='https://raw.githubusercontent.com/interlark/ustvgo-tvguide/master',
        help='Base URL'
    )
    parser.add_argument(
        '--icons-for-light-bg', action='store_true',
        help='Put channel icons adapted for light background'
    )
    parser.add_argument(
        '--version', '-v', action='version', version=f'%(prog)s {VERSION}'
    )
    args = parser.parse_args()

    if args.parallel <= 0 or args.images_size <= 0 or args.images_quality <= 0:
        parser.error('Invalid arguments')

    asyncio.run(download_and_make_epg(**vars(args)))


if __name__ == '__main__':
    main()
