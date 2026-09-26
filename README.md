# Description

`cplay` is a minimalist music player with a textual user interface
written in Python. It aims to provide a power-user-friendly interface
with simple filelist and playlist control.

Instead of building an elaborate database of your music library, `cplay`
allows you to quickly browse the filesystem and enqueue files,
directories, and playlists.

The original cplay was started by Ulf Betlehem in 1998 and is no longer
maintained. This is a rewrite that aims to stay true to the original
design while evolving with a shifting environment.

![screenshot of cplay with file browser](screenshot.png)

# Requirements

-   [python3](http://www.python.org/)
-   [mpv](https://mpv.io/)
-   For SID files, [sidplayfp](https://github.com/libsidplayfp/sidplayfp)
    (available on Raspberry Pi OS with `sudo apt install sidplayfp`).

SID files are played directly by sidplayfp; mpv and FFmpeg do not need SID
support. cplay shows elapsed time only for SID tracks, uses sidplayfp's
configured song length, and seeks by restarting at a five-second offset.
The volume keys control mpv and do not affect sidplayfp playback.

# GamePi13 controls

When the Waveshare GamePi13 joystick driver exposes the pad as
`/dev/input/js0`, cplay reads it automatically. The D-pad navigates, A
selects/plays, B goes back, X toggles play/pause, Y skips to the next track,
L/R seek backward/forward, Select opens help, and Start switches tabs.
Holding a D-pad direction repeats navigation. Keyboard controls continue to
work as well.

Install and configure the GPIO joystick driver as described in the
[GamePi13 guide](https://www.waveshare.com/wiki/GamePi13), and make sure your
user can read `/dev/input/js0`. For a non-default joystick device, set
`CPLAY_GAMEPI13_DEVICE` to its path before starting cplay.

# Installation

    $ pip install cplay-ng

# Usage

    $ cplay-ng

Press `h` to get a list of available keys.
