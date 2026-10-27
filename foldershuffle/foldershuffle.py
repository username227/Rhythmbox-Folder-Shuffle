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
    """Each physical directory is a folder; nested directories are distinct groups."""
    grouped = defaultdict(list)
    for entry in entries:
        uri = entry.get_string(RB.RhythmDBPropType.LOCATION) or ''
        parsed = urlparse(uri)
        if parsed.scheme != 'file':
            continue
        # URI directory is the grouping key; do not combine folders using tags.
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
                # Avoid finding our own or Album Shuffle's custom buttons.
                if 'album' not in tip and 'folder' not in tip:
                    return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = self._find_shuffle_button(child)
                if found is not None:
                    return found
        return None

    def _draw_icon(self, area, cr):
        """Draw GTK's shuffle glyph and a theme-adaptive folder overlay."""
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
        # CLEAR affects only our isolated icon group, never Rhythmbox's toolbar.
        # Cut out the arrowhead before drawing the folder silhouette.
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
        # The folder inset is transparent, not white or a guessed theme color.
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
            # GTK does not automatically recognize DrawingArea as an icon-only button.
            btn.get_style_context().add_class('image-button')
            if isinstance(shuffle, Gtk.Button) and shuffle.get_style_context().has_class('raised'):
                btn.get_style_context().add_class('raised')
            btn.set_tooltip_text('Shuffle Folders — start a fresh random folder sequence')
            area = Gtk.DrawingArea()
            area.set_size_request(size, size)
            area.connect('draw', self._draw_icon)
            area.connect('style-updated', lambda w: w.queue_draw())
            Gtk.IconTheme.get_default().connect('changed', lambda _theme: area.queue_draw())
            area.connect('state-flags-changed', lambda w, old_flags: w.queue_draw())
            btn.add(area)
            btn.connect('clicked', self.on_start)
            self.button = btn
            if shuffle is not None and isinstance(shuffle.get_parent(), Gtk.Box):
                parent = shuffle.get_parent()
                # Place after other shuffle variants, without moving existing controls.
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
            if not self.active or self.object.props.selected_page is not self.last_source:
                return GLib.SOURCE_REMOVE
            view = self.last_source.get_entry_view()
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
            LOG.exception('Unable to append next folder')
        finally:
            self.replenishing = False
        return GLib.SOURCE_REMOVE
