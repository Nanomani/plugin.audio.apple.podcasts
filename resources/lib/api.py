#!/usr/bin/python
# -*- coding: utf-8 -*-
#
#     Copyright (C) 2012 Tristan Fischer
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#    Corrections v1.2.0:
#      - URLs updated to HTTPS (Apple dropped HTTP)
#      - simplejson made optional (fallback to standard json module)
#      - Network timeout on urlopen (prevents indefinite hang)
#      - Default content_type = 'audio' (video removed)
#      - Removed genre navigation (Apple API disabled, always empty)
#      - HTML cleaned from descriptions: strip_html() applied to all summaries
#      - test() function removed (development helper, not needed in production)
#

# simplejson is optional: fall back to the standard library if not installed
try:
    import simplejson as json
except ImportError:
    import json

import re
import html as html_module

from urllib.parse import quote_plus, urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import feedparser


def strip_html(text):
    """Remove HTML tags, decode entities, and strip non-printable characters.

    Podcast RSS feeds often embed HTML markup in <description> / <summary>
    fields.  Kodi does not render HTML in ListItem info labels, so the raw
    tags show up as literal text.  Some feeds also contain emoji and control
    characters that Kodi's font renderer cannot display, showing them as the
    replacement glyph U+25AF (box with question mark).  This helper produces
    clean plain text suitable for display in any Kodi skin.
    """
    if not text:
        return ''
    # Decode HTML entities first (&amp; &lt; &#39; etc.)
    text = html_module.unescape(text)
    # Replace block-level tags with a space so words don't run together
    text = re.sub(r'<(?:br|p|div|li|tr|h[1-6])[^>]*>', ' ', text,
                  flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Remove C0 control chars (0x00-0x08, 0x0b-0x0c, 0x0e-0x1f) and
    # C1 control chars (0x7f-0x9f) — keep tab (0x09), LF (0x0a), CR (0x0d)
    text = re.sub(u'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    # Remove emoji and all supplementary-plane characters (U+10000 and above).
    # Kodi's default font has no glyphs for these and renders them as boxes.
    text = re.sub(u'[\U00010000-\U0010FFFF]', '', text)
    # Remove BMP symbol blocks that Kodi fonts typically lack:
    #   U+2600-U+26FF  Miscellaneous Symbols
    #   U+2700-U+27BF  Dingbats
    #   U+2B00-U+2BFF  Miscellaneous Symbols and Arrows
    #   U+FE00-U+FE0F  Variation Selectors (emoji modifiers)
    text = re.sub(u'[☀-⛿✀-➿⬀-⯿︀-️]',
                  '', text)
    # Collapse multiple whitespace / newlines into a single space
    text = re.sub(r'\s+', ' ', text).strip()
    return text

MAX_PODCAST_SEARCH_LIST = 200
# Network timeout in seconds (prevents indefinite hang if Apple does not respond)
NETWORK_TIMEOUT = 20

# HTTPS URLs — Apple deprecated HTTP
PODCAST_URL = 'https://itunes.apple.com/lookup?id=%(id)d'
PODCAST_SEARCH_URL = ('https://itunes.apple.com/search?term=%(search_term)s'
                      '&country=%(country)s&media=podcast&limit=%(limit)d')


# I guess these should be enough :)
STOREFRONT_IDS = {
    'AU': 143460,  # Australia
    'BE': 143446,  # Belgium
    'BR': 143503,  # Brazil
    'BG': 143526,  # Bulgaria
    'CA': 143455,  # Canada
    'CL': 143483,  # Chile
    'CN': 143465,  # China
    'FI': 143447,  # Finland
    'FR': 143442,  # France
    'DE': 143443,  # Germany
    'DK': 143458,  # Denmark
    'GR': 143448,  # Greece
    'IT': 143450,  # Italy
    'JP': 143462,  # Japan
    'KR': 143466,  # Korea
    'LU': 143451,  # Luxembourg
    'MX': 143468,  # Mexico
    'NL': 143452,  # Netherlands
    'NO': 143457,  # Norway
    'PL': 143478,  # Poland
    'PT': 143453,  # Portugal
    'RO': 143487,  # Romania
    'RU': 143469,  # Russia
    'ES': 143454,  # Spain
    'SE': 143456,  # Sweden
    'CH': 143459,  # Switzerland
    'TR': 143480,  # Turkey
    'GB': 143444,  # UK
    'US': 143441,  # USA
}


class NetworkError(Exception):
    pass


class NoEnclosureException(Exception):
    pass


class ItunesPodcastApi():

    USER_AGENT = 'XBMC ItunesPodcastApi'

    def __init__(self, country='US', image_quality='600x600'):
        self.set_country(country)
        self.image_quality = image_quality

    def set_country(self, country):
        self.country = country.lower()
        self.storefront_id = STOREFRONT_IDS.get(country.upper(), 143441)

    def set_image_quality(self, quality):
        self.image_quality = quality

    def get_podcast_items(self, podcast_id):

        def __format_date(time_struct):
            date_f = '%02i.%02i.%04i'
            if time_struct:
                return date_f % (
                    time_struct[2], time_struct[1], time_struct[0]
                )
            else:
                return ''

        def __format_size(size_str):
            if size_str and str(size_str).isdigit():
                return int(size_str)
            else:
                return 0

        def __get_enclosure_link(node):
            if isinstance(node, (list, tuple)):
                for item in node:
                    if item.get('href') and item.get('rel') == 'enclosure':
                        return item
            elif isinstance(node, dict):
                if node.get('href') and node.get('rel') == 'enclosure':
                    return node
            raise NoEnclosureException

        def __format_thumb(url):
            if not url:
                return ''
            # Force user-selected quality if it's an Apple-hosted image
            if 'mzstatic.com' in url:
                # Common patterns: .../100x100bb.jpg or .../600x600bb.jpg
                # We replace any 3+ digit dimension with the requested one.
                return re.sub(r'/\d{2,}x\d{2,}', '/%s' % self.image_quality, url)
            return url

        url = PODCAST_URL % {'id': int(podcast_id)}
        data = self.__get_json(url)
        if not data.get('results'):
            raise NoEnclosureException
        podcast_url = data['results'][0]['feedUrl']
        raw_content = self.__urlopen(podcast_url)
        content = feedparser.parse(raw_content)
        fallback_thumb = __format_thumb(content['feed'].get('image', {}).get('href'))
        items = []
        for item in content.entries:
            try:
                link = __get_enclosure_link(item.get('links'))
            except NoEnclosureException:
                continue
            thumb = __format_thumb(item.get('image', {}).get('href')) or fallback_thumb
            items.append({
                'title': strip_html(item.get('title')),
                'summary': strip_html(item.get('summary')),
                'author': item.get('author'),
                'item_url': link['url'],
                'size': __format_size(link.get('length', '')),
                'thumb': thumb,
                'duration': item.get('itunes_duration') or link.get('duration') or '',
                'pub_date': __format_date(item.get('published_parsed')),
                'rights': content['feed'].get('copyright')
            })
        if not items:
            raise NoEnclosureException
        return items

    def search_podcast(self, search_term, limit=50):
        url = PODCAST_SEARCH_URL % ({
            'country': self.country,
            'limit': min(int(limit), MAX_PODCAST_SEARCH_LIST),
            'search_term': quote_plus(search_term)
        })
        data = self.__get_json(url)
        podcasts = self._parse_podcast_search_result(data.get('results', []))
        return podcasts

    def get_podcast_plot(self, podcast_id):
        """Fetch the RSS feed channel description for a podcast.

        Called once when adding a podcast to favourites — the result is stored
        in my_podcasts.json so no extra network request is needed at display time.
        """
        url = PODCAST_URL % {'id': int(podcast_id)}
        data = self.__get_json(url)
        if not data.get('results'):
            return ''
        feed_url = data['results'][0].get('feedUrl', '')
        if not feed_url:
            return ''
        raw_content = self.__urlopen(feed_url)
        feed = feedparser.parse(raw_content).get('feed', {})
        description = (feed.get('summary')
                       or feed.get('description')
                       or feed.get('subtitle')
                       or '')
        return strip_html(description)

    def get_single_podcast(self, podcast_id):
        url = PODCAST_URL % {'id': int(podcast_id)}
        data = self.__get_json(url)
        podcasts = self._parse_podcast_search_result(data.get('results', []))
        if not podcasts:
            raise NetworkError('Podcast introuvable : %s' % podcast_id)
        return podcasts[0]

    def _parse_podcast_search_result(self, node):
        def __format_thumb(item):
            # Apple usually provides artworkUrl600, artworkUrl100, etc.
            # We take the best available and then replace the dimension string.
            url = (item.get('artworkUrl600') or item.get('artworkUrl100') or '')
            if url and 'mzstatic.com' in url:
                return re.sub(r'/\d{2,}x\d{2,}', '/%s' % self.image_quality, url)
            return url

        return [{
            'id': item['collectionId'],
            'title': item.get('collectionName'),
            'name': item.get('collectionName'),
            'author': item.get('artistName'),
            'summary': '',
            'thumb': __format_thumb(item),
            'genre': ' / '.join(g for g in item.get('genres', []) if g != 'Podcasts'),
            'rights': '',
            'release_date': ''
        } for item in node]

    def __get_json(self, url, path=None, params=None):
        response = self.__urlopen(url, path, params)
        json_data = json.loads(response)
        return json_data

    def __urlopen(self, url, path=None, params=None):
        if path:
            url += path
        if params:
            url += '?%s' % urlencode(params)
        req = Request(url)
        req.add_header('X-Apple-Store-Front', self.storefront_id)
        req.add_header('User-Agent', self.USER_AGENT)
        try:
            # Timeout prevents indefinite hang if the server is unresponsive
            response = urlopen(req, timeout=NETWORK_TIMEOUT).read()
        except HTTPError as error:
            raise NetworkError('HTTPError: %s' % error)
        except URLError as error:
            raise NetworkError('URLError: %s' % error)
        return response
