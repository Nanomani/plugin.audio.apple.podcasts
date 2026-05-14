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
#    Corrections v1.4.0:
#      - Removed xbmcswift2 dependency: native Kodi API throughout
#      - watch_item calls xbmcplugin.setResolvedUrl() with a ListItem that
#        has setArt({'thumb': ...}), fixing Player.Art(thumb) during playback
#      - Robust JSON storage (atomic write + backup + auto-recovery)
#      - Removed Video Podcasts and genre navigation (APIs no longer available)
#      - Python 3 compatible
#      - Apple URLs updated to HTTPS
#      - HTML tags stripped from descriptions (Plot)
#      - content_type URL parameter removed (audio-only addon)
#      - podcast_title passed to episodes: ListItem.Property(podcast_title)
#        and VideoPlayer.TVShowTitle during playback
#

import os
import sys
import json
import tempfile
import shutil
import re
from urllib.parse import urlencode, parse_qsl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.api import ItunesPodcastApi, NetworkError, NoEnclosureException

# ---------------------------------------------------------------------------
# Plugin globals
# ---------------------------------------------------------------------------

ADDON    = xbmcaddon.Addon()
HANDLE   = int(sys.argv[1])
BASE_URL = sys.argv[0]

STRINGS = {
    'show_my_podcasts':       30003,
    'search_podcast':         30004,
    'add_to_my_podcasts':     30010,
    'remove_from_my_podcasts':30011,
    'network_error':          30200,
    'no_media_found':         30007,
}


def _(string_id):
    if string_id in STRINGS:
        return ADDON.getLocalizedString(STRINGS[string_id])
    xbmc.log('apple_podcasts: missing string %s' % string_id, xbmc.LOGWARNING)
    return string_id


def build_url(**kwargs):
    """Build a plugin:// URL with the given query parameters."""
    return BASE_URL + '?' + urlencode(kwargs)


def get_setting_bool(key):
    """Read a boolean setting, compatible with Kodi 17 and later."""
    try:
        return ADDON.getSettingBool(key)
    except AttributeError:
        return ADDON.getSetting(key).lower() in ('true', '1', 'yes')


# ---------------------------------------------------------------------------
# Robust JSON storage
# ---------------------------------------------------------------------------

class SafeJsonStorage:
    """
    Replaces plugin.get_storage() from xbmcswift2 with an implementation
    that does not corrupt the file on abrupt Kodi shutdown.

    Mechanism:
      - Atomic write: data is written to a temp file first, then renamed
        via os.replace() to the final file. rename is atomic on all common
        filesystems: the file is either the old or the new version, never
        half-written.
      - Backup: before each save the current valid file is copied to .bak.
      - Recovery: on load, if the main file is corrupt the .bak is tried
        automatically; if both fail we start with an empty dict (favorites
        lost, but the addon keeps working).
    """

    def __init__(self, filename):
        profile = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
        if not xbmcvfs.exists(profile):
            xbmcvfs.mkdirs(profile)
        self._path = os.path.join(profile, filename)
        self._backup_path = self._path + '.bak'
        self._data = self._load()

    def _load(self):
        """Load from main file, or backup on error."""
        for filepath in (self._path, self._backup_path):
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    xbmc.log(
                        'SafeJsonStorage: loaded from %s' % filepath,
                        xbmc.LOGDEBUG
                    )
                    return data
                except (ValueError, IOError) as err:
                    xbmc.log(
                        'SafeJsonStorage: corrupt file %s (%s), trying backup'
                        % (filepath, err),
                        xbmc.LOGWARNING
                    )
        xbmc.log('SafeJsonStorage: starting with empty storage', xbmc.LOGINFO)
        return {}

    # --- Dict-like interface ---

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        key = str(key)
        return key in self._data

    def __getitem__(self, key):
        key = str(key)
        return self._data[key]

    def __setitem__(self, key, value):
        key = str(key)
        self._data[key] = value

    def __delitem__(self, key):
        key = str(key)
        del self._data[key]

    def sync(self):
        """
        Atomic save:
          1. Write to a temp file (same directory as the target)
          2. Copy current file to .bak
          3. os.replace(): atomic rename temp -> final file
        """
        dir_path = os.path.dirname(self._path)
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            if os.path.exists(self._path):
                try:
                    shutil.copy2(self._path, self._backup_path)
                except Exception as bak_err:
                    xbmc.log(
                        'SafeJsonStorage: backup failed (%s)' % bak_err,
                        xbmc.LOGWARNING
                    )
            os.replace(tmp_path, self._path)
            tmp_path = None
            xbmc.log('SafeJsonStorage: save successful', xbmc.LOGDEBUG)
        except Exception as err:
            xbmc.log(
                'SafeJsonStorage: save error: %s' % err,
                xbmc.LOGERROR
            )
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

api         = ItunesPodcastApi()
my_podcasts = SafeJsonStorage('my_podcasts.json')

# Storage key for favourites — always 'audio' (video podcasts removed).
_STORAGE_KEY = 'audio'


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def show_root():
    """Main menu."""
    folder_icon = 'DefaultFolder.png'
    entries = [
        (build_url(action='show_my_podcasts'), _('show_my_podcasts')),
        (build_url(action='search'),           _('search_podcast')),
    ]
    items = []
    for url, label in entries:
        li = xbmcgui.ListItem(label)
        li.setArt({'icon': folder_icon, 'thumb': folder_icon})
        items.append((url, li, True))
    xbmcplugin.addDirectoryItems(HANDLE, items, len(items))
    xbmcplugin.endOfDirectory(HANDLE)


def show_items(params):
    """List episodes for a podcast."""
    podcast_id   = params['podcast_id']
    podcast_title = params.get('podcast_title', '')
    podcast_genre = params.get('podcast_genre', '')
    try:
        podcast_items = api.get_podcast_items(podcast_id=podcast_id)
    except NoEnclosureException:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo('name'), _('no_media_found')
        )
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    _add_podcast_items(podcast_id, podcast_items, podcast_title, podcast_genre)


def watch_item(params):
    """
    Resolve a playable episode URL.
    Building the ListItem here (instead of letting xbmcswift2 do it) lets us
    call setArt() and setInfo() so that Player.Art(thumb) and
    VideoPlayer.Plot are populated during playback.
    podcast_title is exposed via VideoPlayer.TVShowTitle during playback.
    """
    item_url     = params['item_url']
    thumb        = params.get('thumb', '')
    title        = params.get('title', '')
    plot         = params.get('plot', '')
    podcast_title = params.get('podcast_title', '')
    podcast_genre = params.get('podcast_genre', '')
    li = xbmcgui.ListItem(label=title, path=item_url)
    li.setArt({
        'thumb':  thumb,
        'fanart': ADDON.getAddonInfo('fanart'),
    })
    if plot or title or podcast_title:
        li.setInfo('video', {
            'title':       title,
            'plot':        plot,
            'tvshowtitle': podcast_title,
            'genre':       podcast_genre,
        })
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def show_my_podcasts(params):
    """List saved podcasts."""
    podcasts = my_podcasts.get(_STORAGE_KEY, {}).values()
    _add_podcasts(podcasts)


def add_to_my_podcasts(params):
    """Save a podcast to favourites, including its RSS channel description."""
    podcast_id = params['podcast_id']
    podcast = api.get_single_podcast(podcast_id=podcast_id)
    # Fetch the channel description from the RSS feed (once at add time).
    # Stored as 'plot' so _add_podcasts can use it without any extra request.
    try:
        podcast['plot'] = api.get_podcast_plot(podcast_id=podcast_id)
    except Exception:
        podcast['plot'] = ''
    if _STORAGE_KEY not in my_podcasts:
        my_podcasts[_STORAGE_KEY] = {}
    my_podcasts[_STORAGE_KEY][podcast_id] = podcast
    my_podcasts.sync()


def del_from_my_podcasts(params):
    """Remove a podcast from favourites and refresh the container."""
    podcast_id = params['podcast_id']
    if podcast_id in my_podcasts.get(_STORAGE_KEY, {}):
        del my_podcasts[_STORAGE_KEY][podcast_id]
        my_podcasts.sync()
        xbmc.executebuiltin('Container.Refresh')


def search(params):
    """Show keyboard, then render search results directly."""
    kb = xbmc.Keyboard('', _('search_podcast'))
    kb.doModal()
    if kb.isConfirmed() and kb.getText():
        search_string = kb.getText()
        num = int(ADDON.getSetting('num_podcasts_search') or '100')
        podcasts = api.search_podcast(search_term=search_string, limit=num)
        _add_podcasts(podcasts, cache=False)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_duration(duration_str):
    """Convert a duration string (HH:MM:SS, MM:SS, or raw seconds) to integer seconds."""
    if not duration_str:
        return 0
    try:
        parts = str(duration_str).strip().split(':')
        if len(parts) == 1:
            return int(parts[0])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) >= 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, AttributeError):
        pass
    return 0


def _pub_date_to_year(pub_date):
    """Extract the year (int) from a DD.MM.YYYY date string."""
    if pub_date and len(pub_date) == 10:
        try:
            return int(pub_date.split('.')[2])
        except (ValueError, IndexError):
            pass
    return 0


def _pub_date_to_dateadded(pub_date):
    """Convert DD.MM.YYYY to the YYYY-MM-DD HH:MM:SS format expected by Kodi dateadded."""
    if pub_date and len(pub_date) == 10:
        try:
            d, m, y = pub_date.split('.')
            return '%s-%s-%s 00:00:00' % (y, m, d)
        except (ValueError, IndexError):
            pass
    return ''


def _add_podcasts(podcasts, cache=True):
    my_podcasts_ids = my_podcasts.get(_STORAGE_KEY, {}).keys()
    items = []
    for i, podcast in enumerate(podcasts):
        podcast_id = str(podcast['id'])
        thumb = podcast['thumb'] or ''

        li = xbmcgui.ListItem(label=podcast['name'])
        li.setArt({'thumb': thumb, 'icon': thumb})
        li.setInfo('video', {
            'title':   podcast['name'],
            'count':   i,
            'plot':    podcast.get('plot') or podcast.get('summary') or '',
            'studio':  podcast['author'] or '',
            'genre':   podcast['genre'] or '',
            'tagline': podcast['rights'] or '',
            'date':    podcast['release_date'] or '',
        })

        if podcast_id not in my_podcasts_ids:
            ctx_label = _('add_to_my_podcasts')
            ctx_url   = build_url(
                action='add_to_my_podcasts',
                podcast_id=podcast_id
            )
        else:
            ctx_label = _('remove_from_my_podcasts')
            ctx_url   = build_url(
                action='del_from_my_podcasts',
                podcast_id=podcast_id
            )
        li.addContextMenuItems([(ctx_label, 'RunPlugin(%s)' % ctx_url)])

        url = build_url(
            action='show_items',
            podcast_id=podcast_id,
            podcast_title=podcast['name'] or '',
            podcast_genre=podcast['genre'] or ''
        )
        items.append((url, li, True))

    xbmcplugin.setContent(HANDLE, 'videos')
    # xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_DATE)
    xbmcplugin.addDirectoryItems(HANDLE, items, len(items))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=cache)


def _add_podcast_items(podcast_id, podcast_items, podcast_title='', podcast_genre=''):
    items = []
    for i, item in enumerate(podcast_items):
        thumb = item['thumb'] or ''

        li = xbmcgui.ListItem(label=item['title'])
        li.setArt({'thumb': thumb, 'icon': thumb})
        li.setInfo('video', {
            'title':       item['title'],
            'count':       i,
            'plot':        item['summary'] or '',
            'studio':      item['author'] or '',
            'size':        item['size'] or 0,
            'date':        item['pub_date'] or '',
            'tagline':     item['rights'] or '',
            'duration':    _parse_duration(item.get('duration')),
            'year':        _pub_date_to_year(item['pub_date'] or ''),
            'dateadded':   _pub_date_to_dateadded(item['pub_date'] or ''),
            'premiered':   _pub_date_to_dateadded(item['pub_date'] or '').split(' ')[0],
            'tvshowtitle': podcast_title,
            'genre':       podcast_genre,
        })
        li.setProperty('IsPlayable', 'true')
        # Expose the parent podcast name as a custom property (accessible in
        # directory skins via $INFO[ListItem.Property(podcast_title)]).
        if podcast_title:
            li.setProperty('podcast_title', podcast_title)
        if podcast_genre:
            li.setProperty('podcast_genre', podcast_genre)

        # thumb, title, plot and podcast_title are passed as query params so
        # watch_item can inject them into the resolved ListItem, populating
        # Player.Art(thumb), VideoPlayer.Plot and VideoPlayer.TVShowTitle.
        url = build_url(
            action='watch_item',
            podcast_id=podcast_id,
            item_url=item['item_url'],
            thumb=thumb,
            title=item['title'] or '',
            plot=item['summary'] or '',
            podcast_title=podcast_title,
            podcast_genre=podcast_genre
        )
        items.append((url, li, False))

    xbmcplugin.setContent(HANDLE, 'videos')
    # DATE first → default sort; other options as secondary.
    # xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_DATE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_DURATION)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_SIZE)
    xbmcplugin.addDirectoryItems(HANDLE, items, len(items))
    xbmcplugin.endOfDirectory(HANDLE)
    
    # Set descending direction so the most recent episodes appear first.
    # xbmc.executebuiltin('Container.SetSortDirection(Descending)')


def _get_country():
    """Detect country from Kodi language on first run, then cache in settings."""
    if not ADDON.getSetting('country_already_set'):
        lang_country_mapping = (
            ('chin',  'CN'),
            ('denm',  'DK'),
            ('fin',   'FI'),
            ('fre',   'FR'),
            ('germa', 'DE'),
            ('greec', 'GR'),
            ('ital',  'IT'),
            ('japa',  'JP'),
            ('kor',   'KR'),
            ('dutch', 'NL'),
            ('norw',  'NO'),
            ('pol',   'PL'),
            ('port',  'PT'),
            ('roma',  'RO'),
            ('russ',  'RU'),
            ('span',  'ES'),
            ('swed',  'SE'),
            ('turk',  'TR'),
            ('engl',  'US'),
        )
        country = None
        xbmc_language = xbmc.getLanguage().lower()
        for lang, country_code in lang_country_mapping:
            if xbmc_language.startswith(lang):
                country = country_code
                ADDON.setSetting('country', country)
                break
        if not country:
            ADDON.openSettings()
    country = ADDON.getSetting('country') or 'US'
    ADDON.setSetting('country_already_set', '1')
    return country


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    country = _get_country()
    api.set_country(country=country)
    api.set_image_quality(ADDON.getSetting('image_quality') or '600x600')

    params = dict(parse_qsl(sys.argv[2].lstrip('?')))
    action = params.get('action', '')

    try:
        if not action:
            show_root()
        elif action == 'show_my_podcasts':
            show_my_podcasts(params)
        elif action == 'show_items':
            show_items(params)
        elif action == 'watch_item':
            watch_item(params)
        elif action == 'add_to_my_podcasts':
            add_to_my_podcasts(params)
        elif action == 'del_from_my_podcasts':
            del_from_my_podcasts(params)
        elif action == 'search':
            search(params)
        else:
            xbmc.log('apple_podcasts: unknown action %r' % action, xbmc.LOGWARNING)
            show_root()
    except NetworkError:
        xbmcgui.Dialog().notification(
            ADDON.getAddonInfo('name'), _('network_error')
        )
