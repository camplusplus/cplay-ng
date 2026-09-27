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

On Raspberry Pi OS Bookworm Lite, sidplayfp sends audio through the system
audio output. To use a Raspberry Pi 3's 3.5 mm jack, select **Headphones**
in `sudo raspi-config` under **System Options > Audio**, then reboot if
requested. If the jack is still silent, check that `/boot/firmware/config.txt`
contains `dtparam=audio=on`, and select analog output with:

    sudo amixer cset numid=3 1

Test the sound path independently of cplay with
`sidplayfp -v /path/to/song.sid`. Verbose output reports which audio driver
sidplayfp uses. If this direct test is silent too, check the Pi's selected
output and mixer volume; cplay does not choose or reroute the ALSA device.

# GamePi13 controls

When the Waveshare GamePi13 GPIO driver exposes the pad as a Linux input
device, cplay looks for its `GPIO Controller` joystick automatically. It
reads `/dev/input/js*`, and can fall back to `/dev/input/event*` if the
joystick interface is unavailable. The D-pad navigates, A selects/plays, B
goes back, X toggles play/pause, Y skips to the next track, L/R seek
backward/forward, Select opens help, and Start switches tabs. Holding a D-pad
direction repeats navigation. Keyboard controls continue to work as well.

Install and configure the GPIO joystick driver as described in the
[GamePi13 guide](https://www.waveshare.com/wiki/GamePi13), then reboot and
check that Linux created a GamePi input device:

    cat /proc/bus/input/devices
    ls -l /dev/input/

The device should be named `GPIO Controller 1`. Your user must have read
permission for its `/dev/input/js*` or `/dev/input/event*` node. If no
`GPIO Controller` device appears, the driver is not loaded or did not build
for the installed Bookworm kernel; cplay cannot read the GPIO buttons until
the driver is working. To check whether Linux receives button events, install
`evtest` and run `sudo evtest /dev/input/eventN` for the GamePi device shown
by the commands above. If Linux sees the events but cplay does not, make sure
you are running this updated cplay version and that your user can read the
device node. For a non-default device path, set `CPLAY_GAMEPI13_DEVICE`
before starting cplay.

# Installation

    $ pip install cplay-ng

# Usage

    $ cplay-ng

Press `h` to get a list of available keys.
