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
support. cplay shows elapsed time only for SID tracks, limit playback duration per SID subtune to 1 loop - use hvsc Song Lenght MD5,
cplay waits two seconds before starting the next playlist file, allowing ALSA
to release the audio device.
The volume keys control mpv and do not affect sidplayfp playback.
If a SID file does not advance, run cplay with `CPLAY_SID_DEBUG=1 cplay-ng`
to show each sidplayfp command and any error output.

On Raspberry Pi OS Bookworm Lite, sidplayfp sends audio through the system
audio output. To use a Raspberry Pi 3's 3.5 mm jack, select **Headphones**
in `sudo raspi-config` under **System Options > Audio**, then reboot if
requested. If the jack is still silent, check that `/boot/firmware/config.txt`
contains `dtparam=audio=on`, and select analog output with:

    sudo amixer cset numid=3 1
If necessary add snd_bcm2835.enable_headphones=1 to cmdline.txt in boot partition (or folder /boot/firmware/cmdline.txt)
Test the sound path independently of cplay with
`sidplayfp -v /path/to/song.sid`. Verbose output reports which audio driver
sidplayfp uses. If this direct test is silent too, check the Pi's selected
output and mixer volume; cplay does not choose or reroute the ALSA device.

Added 'mod', 'xm', 's3m', 'it' file playback support via ffmpeg demuxers.

# GamePi13 controls

For the GamePi13 GPIO buttons, cplay uses `gpiozero` directly, so no
`mk_arcade_joystick_rpi` kernel module is required. On Raspberry Pi OS
Bookworm Lite, install it with:

    sudo apt install python3-gpiozero

cplay uses BCM GPIO pins with pull-ups and 50 ms debounce, matching the
`Button(pin, pull_up=True, bounce_time=0.05)` setup. The mapping is GPIO 5/6
for Up/Down, 16/13 for Left/Right, 21 for A, 20 for B, 15 for X, 12 for Y,
23 for L, 14 for R, 19 for Select, and 26 for Start. The D-pad navigates, A
selects/plays, B goes back, X toggles play/pause, Y skips, L/R seek, Select
opens help, and Start switches tabs. Keyboard controls continue to work.

If a GamePi13 joystick device is present, cplay can read that instead. To
select a specific Linux input device, set `CPLAY_GAMEPI13_DEVICE` before
starting cplay.

# Installation

    $ pip install cplay-ng

# Usage

    $ cplay-ng

Press `h` to get a list of available keys.
Starting an audio file from either the file browser or the playlist replaces
the playlist with playable audio files in that file's directory, beginning
at the selected track and continuing in filename order.
