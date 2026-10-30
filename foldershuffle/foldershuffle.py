"""Continuous Folder Shuffle for Rhythmbox 3.5.x."""
import logging
import random
from collections import defaultdict, deque
from urllib.parse import urlparse, unquote

import cairo

from gi.repository import GObject, Gio, GLib, Gtk, Gdk, GdkPixbuf, Peas, RB

LOG = logging.getLogger('rhythmbox-folder-shuffle')
PLAYLIST_NAME = 'Continuous Folder Shuffle'
BATCH_FOLDERS = 12


def group_folders(entries):
    grouped = defaultdict(list)
    for entry in entries:
        uri = entry.get_string(RB.RhythmDBPropType.LOCATION) or ''
        parsed = urlparse(uri)
        if parsed.scheme != 'file':
            continue
        folder = uri.rsplit('/', 1)[0]
        grouped[folder].append(entry)
    for tracks in grouped.values():
        tracks.sort(key=lambda e: (
            int(e.get_ulong(RB.RhythmDBPropType.DISC_NUMBER)),
            int(e.get_ulong(RB.RhythmDBPropType.TRACK_NUMBER)),
            (e.get_string(RB.RhythmDBPropType.LOCATION) or '').casefold()))
    return grouped


class ContinuousFolderShuffle(GObject.Object, Peas.Activatable):
    __gtype_name__ = 'ContinuousFolderShufflePlugin'
    object = GObject.Property(type=GObject.Object)

    def __init__(self):
        super().__init__()
        self.app = self.player = self.playlist = None
        self.action = self.handler = None
        self.button = self.button_container = self.fallback_row = None
        self.last_source = None
        self.folders = {}
        self.deck = deque()
        self.folder_ends = set()
        self.scheduled_count = self.completed_count = 0
        self.active = self.replenishing = False
        self.pending_follow = None

    def do_activate(self):
        self.app = Gio.Application.get_default()
        self.player = self.object.props.shell_player
        self.action = Gio.SimpleAction.new('folder-shuffle-start', None)
        self.action.connect('activate', self.on_start)
        self.app.add_action(self.action)
        self.app.add_plugin_menu_item('tools', 'folder-shuffle-start',
            Gio.MenuItem.new('Start / Reshuffle Folders', 'app.folder-shuffle-start'))
        self.handler = self.player.connect('playing-song-changed', self.on_song_changed)
        self._install_button()

    def do_deactivate(self):
        self.active = False
        if self.pending_follow is not None:
            GLib.source_remove(self.pending_follow)
            self.pending_follow = None
        if self.button is not None:
            self.button.destroy()
        if self.fallback_row is not None:
            self.fallback_row.destroy()
        self.button = self.button_container = self.fallback_row = None
        if self.handler is not None:
            self.player.disconnect(self.handler)
            self.handler = None
        if self.app is not None:
            self.app.remove_plugin_menu_item('tools', 'folder-shuffle-start')
            self.app.remove_action('folder-shuffle-start')

    def _find_shuffle_button(self, widget):
        if isinstance(widget, Gtk.Button):
            action = widget.get_action_name() if isinstance(widget, Gtk.Actionable) else None
            tip = (widget.get_tooltip_text() or '').lower()
            name = (widget.get_name() or '').lower()
            if action in ('app.play-shuffle', 'app.play-shuffle(true)', 'app.play-shuffle(false)'):
                return widget
            if 'shuffle' in tip or 'shuffle' in name:
                if 'album' not in tip and 'folder' not in tip:
                    return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = self._find_shuffle_button(child)
                if found is not None:
                    return found
        return None

    def _draw_icon(self, area, cr):
        size = min(area.get_allocated_width(), area.get_allocated_height())
        if size < 2:
            return False
        theme = Gtk.IconTheme.get_default()
        pixbuf = None
        for name in ('media-playlist-shuffle-symbolic', 'media-playlist-shuffle'):
            if not theme.has_icon(name):
                continue
            try:
                info = theme.lookup_icon(name, size, Gtk.IconLookupFlags.FORCE_SIZE)
                if info is None:
                    continue
                pixbuf, _symbolic = info.load_symbolic_for_context(area.get_style_context())
                break
            except GLib.Error:
                continue
        cr.push_group()
        if pixbuf:
            x = (area.get_allocated_width() - pixbuf.get_width()) / 2
            y = (area.get_allocated_height() - pixbuf.get_height()) / 2
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, x, y)
            cr.paint()
        fg = area.get_style_context().get_color(area.get_style_context().get_state())
        dark = (fg.red, fg.green, fg.blue)
        x, y = size*.45, size*.46
        w, h = size*.51, size*.54
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.rectangle(x-size*.035, y-size*.035, w+size*.065, h+size*.035)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgb(*dark)
        cr.move_to(x, y+size*.06)
        cr.line_to(x, y)
        cr.line_to(x+w*.37, y)
        cr.line_to(x+w*.49, y+size*.085)
        cr.line_to(x+w, y+size*.085)
        cr.line_to(x+w, y+h)
        cr.line_to(x, y+h)
        cr.close_path()
        cr.fill()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.rectangle(x+size*.055, y+size*.145, w-size*.10, h-size*.215)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.pop_group_to_source()
        cr.paint()
        return False

    def _install_button(self):
        try:
            window = self.object.props.window
            shuffle = self._find_shuffle_button(window)
            size = 20
            if shuffle:
                def find_image(widget):
                    if isinstance(widget, Gtk.Image):
                        return widget
                    if isinstance(widget, Gtk.Container):
                        for c in widget.get_children():
                            found = find_image(c)
                            if found is not None:
                                return found
                    return None
                img = find_image(shuffle)
                if img and img.get_pixel_size() > 0:
                    size = img.get_pixel_size()
            btn = Gtk.Button()
            if isinstance(shuffle, Gtk.Button):
                btn.set_relief(shuffle.get_relief())
            btn.get_style_context().add_class('image-button')
            if isinstance(shuffle, Gtk.Button) and shuffle.get_style_context().has_class('raised'):
                btn.get_style_context().add_class('raised')
            btn.set_tooltip_text('Shuffle Folders — start a fresh random folder sequence')
            area = Gtk.DrawingArea()
            area.set_size_request(size, size)
            area.connect('draw', self._draw_icon)
            area.connect('style-updated', lambda w: area.queue_draw())
            Gtk.IconTheme.get_default().connect('changed', lambda _theme: area.queue_draw())
            area.connect('state-flags-changed', lambda w, old_flags: area.queue_draw())
            btn.add(area)
            btn.connect('clicked', self.on_start)
            self.button = btn
            if shuffle is not None and isinstance(shuffle.get_parent(), Gtk.Box):
                parent = shuffle.get_parent()
                children = parent.get_children()
                pos = children.index(shuffle) + 1
                while pos < len(children) and isinstance(children[pos], Gtk.Button) and (
                        'shuffle albums' in (children[pos].get_tooltip_text() or '').lower()):
                    pos += 1
                parent.pack_start(btn, False, False, 0)
                parent.reorder_child(btn, pos)
                self.button_container = parent
            else:
                titlebar = window.get_titlebar()
                if isinstance(titlebar, Gtk.HeaderBar):
                    titlebar.pack_end(btn)
                    self.button_container = titlebar
                else:
                    root = window.get_child()
                    if not isinstance(root, Gtk.Box):
                        btn.destroy()
                        self.button = None
                        return
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    row.pack_end(btn, False, False, 0)
                    root.pack_start(row, False, False, 0)
                    root.reorder_child(row, 0)
                    self.fallback_row = row
                    self.button_container = row
                    row.show_all()
            btn.show_all()
        except Exception:
            LOG.exception('Folder Shuffle button could not be installed; Tools menu remains available')

    def _source(self):
        page = self.object.props.selected_page
        if page is self.playlist and self.last_source is not None:
            page = self.last_source
        if page is None or not isinstance(page, RB.Source) or page is self.playlist:
            return None
        return page

    def _get_playlist(self):
        if self.playlist is not None:
            return self.playlist
        manager = self.object.props.playlist_manager
        for playlist in manager.get_playlists():
            if isinstance(playlist, RB.StaticPlaylistSource) and playlist.props.name == PLAYLIST_NAME:
                self.playlist = playlist
                return playlist
        self.playlist = manager.new_playlist(PLAYLIST_NAME, False)
        return self.playlist

    def _append_folder(self):
        if not self.deck:
            keys = list(self.folders)
            random.SystemRandom().shuffle(keys)
            self.deck = deque(keys)
        key = self.deck.popleft()
        tracks = self.folders[key]
        self.scheduled_count += 1
        for entry in tracks:
            self.playlist.add_entry(entry, -1)
        self.folder_ends.add(tracks[-1].get_string(RB.RhythmDBPropType.LOCATION))
        return tracks[0]

    def on_start(self, *_args):
        try:
            source = self._source()
            if source is None:
                LOG.warning('Folder Shuffle: select Music or a playlist first')
                return
            view = source.get_entry_view()
            model = view.props.model if view is not None else None
            if model is None:
                LOG.warning('Folder Shuffle: no displayed song list')
                return
            grouped = group_folders(row[0] for row in model)
            if not grouped:
                LOG.warning('Folder Shuffle: no local files in current filtered view')
                return
            self.active = False
            self.player.set_playback_state(False, False)
            playlist = self._get_playlist()
            old = playlist.get_query_model()
            if old is not None:
                for entry in [r[0] for r in old]:
                    playlist.remove_entry(entry)
            self.folders = grouped
            self.deck = deque(grouped)
            shuffled = list(self.deck)
            random.SystemRandom().shuffle(shuffled)
            self.deck = deque(shuffled)
            self.folder_ends = set()
            self.scheduled_count = self.completed_count = 0
            self.last_source = source
            first = None
            for _ in range(min(BATCH_FOLDERS, len(self.deck))):
                entry = self._append_folder()
                if first is None:
                    first = entry
            self.active = True
            self.player.play_entry(first, playlist)
            GLib.idle_add(self._restore_view, source, first)
            LOG.warning('Folder Shuffle: %d folders started', len(grouped))
        except Exception:
            self.active = False
            LOG.exception('Could not start Folder Shuffle')

    def _restore_view(self, source, entry):
        try:
            if self.object.props.selected_page is not source:
                self.object.props.display_page_tree.select(source)
            self._follow_later(entry)
        except Exception:
            LOG.exception('Could not restore browser view')
        return GLib.SOURCE_REMOVE

    def _follow_later(self, entry):
        if self.pending_follow is not None:
            GLib.source_remove(self.pending_follow)
        self.pending_follow = GLib.idle_add(self._follow, entry)

    def _follow(self, entry):
        self.pending_follow = None
        try:
            # Follow the playing row in either the original view or generated playlist.
            source = self.object.props.selected_page
            if not self.active or source not in (self.last_source, self.playlist):
                return GLib.SOURCE_REMOVE
            view = source.get_entry_view()
            if view is not None and view.get_entry_contained(entry):
                view.select_none()
                view.select_entry(entry)
                view.scroll_to_entry(entry)
        except Exception:
            LOG.exception('Could not follow playing track')
        return GLib.SOURCE_REMOVE

    def on_song_changed(self, _player, entry):
        if not self.active or entry is None or self.replenishing:
            return
        try:
            if self.player.get_playing_source() != self.playlist:
                self.active = False
                return
            self._follow_later(entry)
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri not in self.folder_ends:
                return
            self.folder_ends.discard(uri)
            self.completed_count += 1
            if not self.deck and self.completed_count < self.scheduled_count:
                return
            self.replenishing = True
            GLib.idle_add(self._append_idle)
        except Exception:
            LOG.exception('Folder Shuffle continuation failed')

    def _append_idle(self):
        try:
            if self.active and self.playlist is not None:
                self._append_folder()
        except Exception:
            LOG.exception('Unable to append next random folder')
        finally:
            self.replenishing = False
        return GLib.SOURCE_REMOVE


# === Rhythmbox persistent shuffle session integration (v1) ===

# This file's session extension is embedded, so this shuffle plugin remains
# independently installable with or without Session Restore.
import json as _ss_json
import os as _ss_os
import tempfile as _ss_tempfile
import time as _ss_time
import uuid as _ss_uuid
from pathlib import Path as _SSPath


def _ss_root():
    return _SSPath(_ss_os.environ.get('XDG_STATE_HOME',
                    _ss_os.path.expanduser('~/.local/state'))) / 'rhythmbox-shuffle-state'


def _ss_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with _ss_tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent),
                                             prefix='.' + path.name + '.', delete=False) as fp:
            name = fp.name
            _ss_json.dump(data, fp, ensure_ascii=False, separators=(',', ':'))
            fp.write('\n')
            fp.flush()
            _ss_os.fsync(fp.fileno())
        _ss_os.replace(name, path)
    finally:
        if name and _ss_os.path.exists(name):
            _ss_os.unlink(name)


def _ss_read(path):
    try:
        with path.open(encoding='utf-8') as fp:
            value = _ss_json.load(fp)
        return value if isinstance(value, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        LOG.exception('Shuffle session: failed to read %s', path)
        return None


def _ss_key(key):
    return list(key) if isinstance(key, tuple) else key


def _ss_decode(key):
    return tuple(key) if isinstance(key, list) else key


def _ss_paths(mode):
    root = _ss_root()
    return root / (mode + '-groups.json'), root / (mode + '-progress.json'), root / (mode + '-ready.json')


def _ss_progress(plugin, mode):
    if getattr(plugin, '_ss_generation', None) is None:
        return
    try:
        keys = list(plugin.deck)
        ends = getattr(plugin, 'album_ends' if mode == 'album' else 'folder_ends')
        playlist = getattr(plugin, 'playlist', None)
        last_source = getattr(plugin, 'last_source', None)
        state = {'version': 1, 'generation': plugin._ss_generation,
                 'active': bool(plugin.active),
                 'playlist': playlist.props.name if playlist else '',
                 'source': last_source.props.name if last_source else 'Music',
                 'deck': [_ss_key(k) for k in keys],
                 'ends': sorted(ends),
                 'scheduled': int(plugin.scheduled_count),
                 'completed': int(plugin.completed_count),
                 'timestamp': int(_ss_time.time())}
        _ss_atomic(_ss_paths(mode)[1], state)
    except Exception:
        LOG.exception('%s Shuffle: unable to checkpoint progress', mode.title())


def _ss_start(plugin, mode):
    if getattr(plugin, '_ss_restore_timer', None) is not None:
        try:
            GLib.source_remove(plugin._ss_restore_timer)
        except Exception:
            pass
        plugin._ss_restore_timer = None
    plugin._ss_restored = True
    if not plugin.active:
        _ss_progress(plugin, mode)
        return
    try:
        plugin._ss_generation = _ss_uuid.uuid4().hex
        groups = plugin.albums if mode == 'album' else plugin.folders
        payload = {'version': 1, 'generation': plugin._ss_generation,
                   'groups': [{'key': _ss_key(k),
                               'uris': [entry.get_string(RB.RhythmDBPropType.LOCATION)
                                        for entry in entries]}
                              for k, entries in groups.items()]}
        _ss_atomic(_ss_paths(mode)[0], payload)
        _ss_progress(plugin, mode)
        _ss_ready(plugin, mode)
    except Exception:
        LOG.exception('%s Shuffle: failed to save initial shuffle order', mode.title())


def _ss_ready(plugin, mode):
    try:
        _ss_atomic(_ss_paths(mode)[2], {'pid': _ss_os.getpid(),
                   'generation': plugin._ss_generation, 'active': bool(plugin.active)})
    except Exception:
        LOG.exception('%s Shuffle: could not publish readiness', mode.title())


def _ss_restore(plugin, mode):
    if getattr(plugin, '_ss_restored', False):
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    manifest = _ss_read(_ss_paths(mode)[0])
    progress = _ss_read(_ss_paths(mode)[1])
    if manifest is None or progress is None or not progress.get('active'):
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    if manifest.get('generation') != progress.get('generation'):
        LOG.warning('%s Shuffle: inconsistent state generations; not restoring', mode.title())
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    if _ss_time.monotonic() > plugin._ss_deadline:
        LOG.warning('%s Shuffle: library not ready; session state remains on disk', mode.title())
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    try:
        target = None
        for playlist in plugin.object.props.playlist_manager.get_playlists():
            if playlist.props.name == progress.get('playlist'):
                target = playlist
                break
        if target is None or target.get_query_model() is None:
            return GLib.SOURCE_CONTINUE
        database = plugin.object.props.db
        groups = {}
        for group in manifest['groups']:
            key = _ss_decode(group['key'])
            entries = []
            for uri in group['uris']:
                entry = database.entry_lookup_by_location(uri)
                if entry is None:
                    return GLib.SOURCE_CONTINUE  # NAS or DB still loading
                entries.append(entry)
            if entries:
                groups[key] = entries
        if not groups:
            return GLib.SOURCE_CONTINUE
        # Avoid restoring stale progress over a newer shuffle started by user.
        if getattr(plugin, '_ss_restored', False):
            return GLib.SOURCE_REMOVE
        plugin.playlist = target
        if mode == 'album':
            plugin.albums = groups
            plugin.album_ends = set(progress.get('ends', []))
        else:
            plugin.folders = groups
            plugin.folder_ends = set(progress.get('ends', []))
        plugin.deck = deque(k for raw in progress.get('deck', [])
                            if (k := _ss_decode(raw)) in groups)
        plugin.scheduled_count = int(progress.get('scheduled', 0))
        plugin.completed_count = int(progress.get('completed', 0))
        wanted = progress.get('source') or 'Music'
        plugin.last_source = plugin.object.props.library_source
        for source in plugin.object.props.playlist_manager.get_playlists():
            if source.props.name == wanted:
                plugin.last_source = source
                break
        plugin.active = True
        plugin.replenishing = False
        plugin._ss_generation = progress['generation']
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        _ss_ready(plugin, mode)
        LOG.warning('%s Shuffle: recovered %d groups, %d pending groups, %d group ends',
                    mode.title(), len(groups), len(plugin.deck), len(progress.get('ends', [])))
        entry = plugin.player.get_playing_entry()
        if entry and plugin.player.get_playing_source() == target:
            if mode == 'album':
                plugin._queue_follow(entry)
            else:
                plugin._follow_later(entry)
        _ss_schedule_music_follow(plugin, mode)
        return GLib.SOURCE_REMOVE
    except Exception:
        LOG.exception('%s Shuffle: restore attempt encountered error', mode.title())
        return GLib.SOURCE_CONTINUE



# Restore Music-window highlighting independently of shuffle-session recovery.
# A restored playlist already receives Rhythmbox's playing-row indicator, but
# the Music view must be explicitly selected/scrolled when it is visible.
# This has no effect on audio, ordering, playlist membership, or state files.
def _ss_follow_music_view(plugin, mode):
    plugin._ss_music_follow_id = None
    try:
        playing_source = plugin.player.get_playing_source()
        expected_name = 'Continuous Album Shuffle' if mode == 'album' else 'Continuous Folder Shuffle'
        if playing_source is None or playing_source.props.name != expected_name:
            return GLib.SOURCE_REMOVE
        music = plugin.object.props.library_source
        if music is None or plugin.object.props.selected_page != music:
            return GLib.SOURCE_REMOVE
        entry = plugin.player.get_playing_entry()
        if entry is None:
            return GLib.SOURCE_REMOVE
        view = music.get_entry_view()
        if view is not None and view.get_entry_contained(entry):
            view.select_none()
            view.select_entry(entry)
            view.scroll_to_entry(entry)
    except Exception:
        LOG.exception('%s Shuffle: could not highlight playing song in Music', mode.title())
    return GLib.SOURCE_REMOVE


def _ss_schedule_music_follow(plugin, mode):
    if getattr(plugin, '_ss_music_follow_id', None) is None:
        plugin._ss_music_follow_id = GLib.idle_add(_ss_follow_music_view, plugin, mode)

def _ss_wrap(cls, mode):
    original_activate = cls.do_activate
    original_deactivate = cls.do_deactivate
    original_start = cls.on_start
    original_song = cls.on_song_changed
    original_append = cls._append_next_idle if mode == 'album' else cls._append_idle

    def activate(self):
        original_activate(self)
        self._ss_restored = False
        self._ss_generation = None
        self._ss_deadline = _ss_time.monotonic() + 600
        self._ss_restore_timer = GLib.timeout_add_seconds(3, _ss_restore, self, mode)
        GLib.idle_add(lambda: (_ss_restore(self, mode), GLib.SOURCE_REMOVE)[1])
        # Follow the playing row when the user returns to Music, too.
        self._ss_music_follow_id = None
        self._ss_music_page_handler = self.object.connect(
            'notify::selected-page',
            lambda *_args: _ss_schedule_music_follow(self, mode))
        self._ss_music_source_handler = self.player.connect(
            'playing-source-changed',
            lambda *_args: _ss_schedule_music_follow(self, mode))
        _ss_schedule_music_follow(self, mode)

    def deactivate(self):
        if getattr(self, '_ss_music_follow_id', None) is not None:
            GLib.source_remove(self._ss_music_follow_id)
            self._ss_music_follow_id = None
        if getattr(self, '_ss_music_page_handler', None) is not None:
            self.object.disconnect(self._ss_music_page_handler)
            self._ss_music_page_handler = None
        if getattr(self, '_ss_music_source_handler', None) is not None:
            self.player.disconnect(self._ss_music_source_handler)
            self._ss_music_source_handler = None
        _ss_progress(self, mode)  # Must occur BEFORE original sets active=False.
        if getattr(self, '_ss_restore_timer', None) is not None:
            try:
                GLib.source_remove(self._ss_restore_timer)
            except Exception:
                pass
            self._ss_restore_timer = None
        try:
            _ss_paths(mode)[2].unlink(missing_ok=True)
        except OSError:
            LOG.exception('%s Shuffle: unable to clear readiness file', mode.title())
        original_deactivate(self)

    def start(self, *args):
        result = original_start(self, *args)
        _ss_start(self, mode)
        return result

    def song_changed(self, *args):
        result = original_song(self, *args)
        _ss_schedule_music_follow(self, mode)
        _ss_progress(self, mode)
        return result

    def append(self, *args):
        result = original_append(self, *args)
        _ss_progress(self, mode)
        return result

    cls.do_activate = activate
    cls.do_deactivate = deactivate
    cls.on_start = start
    cls.on_song_changed = song_changed
    if mode == 'album':
        cls._append_next_idle = append
    else:
        cls._append_idle = append

_ss_wrap(ContinuousFolderShuffle, 'folder')
