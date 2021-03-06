# coding=utf-8

import io
import logging
import math
import re
import types

import rarfile

from bs4 import BeautifulSoup
from zipfile import ZipFile, is_zipfile
from rarfile import RarFile, is_rarfile
from babelfish import Language, language_converters, Script
from requests import Session
from guessit import guessit
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle
from subliminal_patch.utils import sanitize, fix_inconsistent_naming as _fix_inconsistent_naming
from subliminal.exceptions import ProviderError
from subliminal.score import get_equivalent_release_groups
from subliminal.utils import sanitize_release_group
from subliminal.subtitle import fix_line_ending, guess_matches
from subliminal.video import Episode, Movie

# parsing regex definitions
title_re = re.compile(r'(?P<title>(?:.+(?= [Aa][Kk][Aa] ))|.+)(?:(?:.+)(?P<altitle>(?<= [Aa][Kk][Aa] ).+))?')
lang_re = re.compile(r'(?<=flags/)(?P<lang>.{2})(?:.)(?P<script>c?)(?:.+)')
season_re = re.compile(r'Sezona (?P<season>\d+)')
episode_re = re.compile(r'Epizoda (?P<episode>\d+)')
year_re = re.compile(r'(?P<year>\d+)')
fps_re = re.compile(r'fps: (?P<fps>.+)')


def fix_inconsistent_naming(title):
    """Fix titles with inconsistent naming using dictionary and sanitize them.

    :param str title: original title.
    :return: new title.
    :rtype: str

    """
    return _fix_inconsistent_naming(title, {"DC's Legends of Tomorrow": "Legends of Tomorrow",
                                            "Marvel's Jessica Jones": "Jessica Jones"})

logger = logging.getLogger(__name__)

# Configure :mod:`rarfile` to use the same path separator as :mod:`zipfile`
rarfile.PATH_SEP = '/'

language_converters.register('titlovi = subliminal_patch.converters.titlovi:TitloviConverter')


class TitloviSubtitle(Subtitle):
    provider_name = 'titlovi'

    def __init__(self, language, page_link, download_link, sid, releases, title, alt_title=None, season=None,
                 episode=None, year=None, fps=None, asked_for_release_group=None):
        super(TitloviSubtitle, self).__init__(language, page_link=page_link)
        self.sid = sid
        self.releases = self.release_info = releases
        self.title = title
        self.alt_title = alt_title
        self.season = season
        self.episode = episode
        self.year = year
        self.download_link = download_link
        self.fps = fps
        self.matches = None
        self.asked_for_release_group = asked_for_release_group

    @property
    def id(self):
        return self.sid

    def get_matches(self, video):
        matches = set()

        # handle movies and series separately
        if isinstance(video, Episode):
            # series
            if video.series and sanitize(self.title) == fix_inconsistent_naming(video.series) or sanitize(
                    self.alt_title) == fix_inconsistent_naming(video.series):
                matches.add('series')
            # year
            if video.original_series and self.year is None or video.year and video.year == self.year:
                matches.add('year')
            # season
            if video.season and self.season == video.season:
                matches.add('season')
            # episode
            if video.episode and self.episode == video.episode:
                matches.add('episode')
        # movie
        elif isinstance(video, Movie):
            # title
            if video.title and sanitize(self.title) == fix_inconsistent_naming(video.title) or sanitize(
                    self.alt_title) == fix_inconsistent_naming(video.title):
                matches.add('title')
            # year
            if video.year and self.year == video.year:
                matches.add('year')

        # rest is same for both groups

        # release_group
        if (video.release_group and self.releases and
                any(r in sanitize_release_group(self.releases)
                    for r in get_equivalent_release_groups(sanitize_release_group(video.release_group)))):
            matches.add('release_group')
        # resolution
        if video.resolution and self.releases and video.resolution in self.releases.lower():
            matches.add('resolution')
        # format
        if video.format and self.releases and video.format.lower() in self.releases.lower():
            matches.add('format')
        # other properties
        matches |= guess_matches(video, guessit(self.releases))

        self.matches = matches

        return matches


class TitloviProvider(Provider):
    subtitle_class = TitloviSubtitle
    languages = {Language.fromtitlovi(l) for l in language_converters['titlovi'].codes} | {Language.fromietf('sr')}
    server_url = 'http://titlovi.com'
    search_url = server_url + '/titlovi/?'
    download_url = server_url + '/download/?type=1&mediaid='

    def initialize(self):
        self.session = Session()

    def terminate(self):
        self.session.close()

    def query(self, languages, title, season=None, episode=None, year=None, video=None):
        items_per_page = 10
        current_page = 1

        # convert list of languages into search string
        langs = '|'.join(
            map(str, [l.titlovi if l != Language.fromietf('sr') else 'cirilica' for l in languages]))
        # set query params
        params = {'prijevod': title, 'jezik': langs}
        is_episode = False
        if season and episode:
            is_episode = True
            params['s'] = season
            params['e'] = episode
        if year:
            params['g'] = year

        # loop through paginated results
        logger.info('Searching subtitles %r', params)
        subtitles = []

        while True:
            # query the server
            try:
                r = self.session.get(self.search_url, params=params, timeout=10)
                r.raise_for_status()

                soup = BeautifulSoup(r.content, 'lxml')

                # number of results
                result_count = int(soup.select_one('.results_count b').string)
            except:
                result_count = None

            # exit if no results
            if not result_count:
                if not subtitles:
                    logger.debug('No subtitles found')
                else:
                    logger.debug("No more subtitles found")
                break

            # number of pages with results
            pages = int(math.ceil(result_count / float(items_per_page)))

            # get current page
            if 'pg' in params:
                current_page = int(params['pg'])

            try:
                sublist = soup.select('section.titlovi > ul.titlovi > li')
                for sub in sublist:
                    # subtitle id
                    sid = sub.find(attrs={'data-id': True}).attrs['data-id']
                    # get download link
                    download_link = self.download_url + sid
                    # title and alternate title
                    match = title_re.search(sub.a.string)
                    if match:
                        _title = match.group('title')
                        alt_title = match.group('altitle')
                    else:
                        continue

                    # page link
                    page_link = self.server_url + sub.a.attrs['href']
                    # subtitle language
                    match = lang_re.search(sub.select_one('.lang').attrs['src'])
                    if match:
                        try:
                            lang = Language.fromtitlovi(match.group('lang'))
                            script = match.group('script')
                            if script:
                                lang.script = Script(script)
                        except ValueError:
                            continue

                    # relase year or series start year
                    match = year_re.search(sub.find(attrs={'data-id': True}).parent.i.string)
                    if match:
                        year = int(match.group('year'))
                    # fps
                    match = fps_re.search(sub.select_one('.fps').string)
                    if match:
                        fps = match.group('fps')
                    # releases
                    releases = str(sub.select_one('.fps').parent.contents[0].string)

                    # handle movies and series separately
                    if is_episode:
                        # season and episode info
                        sxe = sub.select_one('.s0xe0y').string
                        if sxe:
                            match = season_re.search(sxe)
                            if match:
                                season = int(match.group('season'))
                            match = episode_re.search(sxe)
                            if match:
                                episode = int(match.group('episode'))

                        subtitle = self.subtitle_class(lang, page_link, download_link, sid, releases, _title,
                                                       alt_title=alt_title, season=season, episode=episode, year=year,
                                                       fps=fps, asked_for_release_group=video.release_group)
                    else:
                        subtitle = self.subtitle_class(lang, page_link, download_link, sid, releases, _title,
                                                       alt_title=alt_title, year=year, fps=fps,
                                                       asked_for_release_group=video.release_group)
                    logger.debug('Found subtitle %r', subtitle)

                    # prime our matches so we can use the values later
                    subtitle.get_matches(video)

                    # add found subtitles
                    subtitles.append(subtitle)

            finally:
                soup.decompose()

            # stop on last page
            if current_page >= pages:
                break

            # increment current page
            params['pg'] = current_page + 1
            logger.debug('Getting page %d', params['pg'])

        return subtitles

    def list_subtitles(self, video, languages):
        season = episode = None
        if isinstance(video, Episode):
            title = video.series
            season = video.season
            episode = video.episode
        else:
            title = video.title

        return [s for s in
                self.query(languages, fix_inconsistent_naming(title), season=season, episode=episode, year=video.year,
                           video=video)]

    def download_subtitle(self, subtitle):
        r = self.session.get(subtitle.download_link, timeout=10)
        r.raise_for_status()

        # open the archive
        archive_stream = io.BytesIO(r.content)
        if is_rarfile(archive_stream):
            logger.debug('Archive identified as rar')
            archive = RarFile(archive_stream)
        elif is_zipfile(archive_stream):
            logger.debug('Archive identified as zip')
            archive = ZipFile(archive_stream)
        else:
            raise ProviderError('Unidentified archive type')

        # extract subtitle's content
        subs_in_archive = []
        for name in archive.namelist():
            for ext in (".srt", ".sub", ".ssa", ".ass"):
                if name.endswith(ext):
                    subs_in_archive.append(name)

        # select the correct subtitle file
        matching_sub = None
        if len(subs_in_archive) == 1:
            matching_sub = subs_in_archive[0]
        else:
            for sub_name in subs_in_archive:
                guess = guessit(sub_name)

                # consider subtitle valid if:
                # - episode and season match
                # - format matches (if it was matched before)
                # - release group matches (and we asked for one and it was matched, or it was not matched)
                if guess["episode"] == subtitle.episode and guess["season"] == subtitle.season:
                    format_matches = True

                    if "format" in subtitle.matches:
                        format_matches = False
                        releases = subtitle.releases.lower()
                        formats = guess["format"]
                        if not isinstance(formats, types.ListType):
                            formats = [formats]

                        for f in formats:
                            format_matches = f.lower() in releases
                            if format_matches:
                                break

                    release_group_matches = True
                    if subtitle.asked_for_release_group and "release_group" in subtitle.matches:
                        asked_for_rlsgrp = subtitle.asked_for_release_group.lower()
                        release_group_matches = False
                        release_groups = guess["release_group"]
                        if not isinstance(release_groups, types.ListType):
                            release_groups = [release_groups]

                        for release_group in release_groups:
                            release_group_matches = release_group.lower() == asked_for_rlsgrp
                            if release_group_matches:
                                break

                    if release_group_matches and format_matches:
                        matching_sub = sub_name
                        break

        if not matching_sub:
            raise ProviderError("None of expected subtitle found in archive")
        subtitle.content = fix_line_ending(archive.read(matching_sub))
