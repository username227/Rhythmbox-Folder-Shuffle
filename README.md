# Rhythmbox-Folder-Shuffle

This plugin adds **Continuous Folder Shuffle** to Rhythmbox, playing entire folders of music in randomized order while preserving track order within each folder.

## Features

- Adds a **Shuffle Folders** button next to Rhythmbox's shuffle button. Folder shuffle can also be started using **Tools → Start / Reshuffle Folders**.
- Each time a folder shuffle is started or restarted, the folder playback order is randomized.
- Playback begins with the first track of a randomly selected folder. Each folder plays in disc and track order before the next folder begins, regardless of the order displayed in the music library.
- Folders do not repeat until all eligible folders have played; then another randomized sequence begins.
- **Every eligible track from the current filtered selection is included in the generated playlist.** There is no arbitrary folder or track-count limit.
- For large music libraries, the playlist is populated incrementally to maintain Rhythmbox responsiveness. Playback can begin before playlist generation is complete.
- The plugin respects the currently displayed library filters. For example, selecting Classical includes only eligible tracks from that genre.
- The current library view and filters are preserved while playing. The playing track is highlighted and scrolled into view when present in the selected view.
- Starting a new shuffle replaces the previously generated playlist. **To preserve an existing playlist, rename it before reshuffling.**
- The plugin saves shuffle-session information to support recovery across Rhythmbox restarts. Exact playback-position restoration is handled separately by the Session Restore plugin.

## Shuffling Methodology

Folder Shuffle is separate from the related [Album Shuffle plugin](https://github.com/username227/Rhythmbox-Album-Shuffle). It groups tracks by their **containing directories**, rather than by Album metadata.

This is particularly useful for Classical music. If an album contains several works stored in separate subfolders, Folder Shuffle plays each work as a separate group. Conversely, if several albums are placed in one folder, they play together.

Folder Shuffle includes tracks with blank or Unknown Album tags automatically; **it has no Album-tag preferences setting**.

Only local `file://` locations are currently supported. This includes network shares mounted into the local filesystem, such as a CIFS mount, when their tracks have `file://` URIs. Other URI schemes are not included.

## Installation

For a **user-level installation**, copy the `foldershuffle` plugin folder into:

`~/.local/share/rhythmbox/plugins/`

For a **system-wide installation**, the location depends on your distribution:

- **Arch Linux:** `/usr/lib/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (x86_64):** `/usr/lib/x86_64-linux-gnu/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (ARM64):** `/usr/lib/aarch64-linux-gnu/rhythmbox/plugins/`

For **Flatpak**, use:

`~/.var/app/org.gnome.Rhythmbox3/data/rhythmbox/plugins/`

Restart Rhythmbox, open **Preferences → Plugins**, and enable the plugin.

## Full Disclosure

I am not a developer and am not formally trained in computer programming. AI was used to assist in the writing of the code based on my specifications, testing, and feedback. I have tested various development versions on Arch Linux with Rhythmbox 3.5.1. I have not tested the plugin on other distributions. Reviews and improvements to the codebase, as well as additional feature requests, are welcome.

### Please Note

The plugin is Python-based. Issues affecting Python-based Rhythmbox plugins have broken them in the past for significant periods of time. Those upstream issues appear to have been resolved, but future changes to Rhythmbox or its dependencies, particularly the Python plugin loader, could break this plugin.
