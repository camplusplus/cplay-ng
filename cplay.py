#!/usr/bin/env python3

import curses
import fcntl
import functools
import glob
import json
import os
import queue
import random
import re
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
from contextlib import ExitStack, contextmanager

__version__ = '5.5.0'

XDG_RUNTIME_DIR = os.getenv('XDG_RUNTIME_DIR', '/tmp')

AUDIO_EXTENSIONS = [
    'mp3', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'm4b', 'wav', 'mid', 'wma',
    'sid',
]
SID_MAX_DURATION = 180
SID_NEXT_TRACK_DELAY = 2
SID_DEBUG = os.getenv('CPLAY_SID_DEBUG') == '1'

HELP = """Global
------
Up, k        : move to previous item
Down, j      : move to next item
PageUp, K    : move to previous page
PageDown, J  : move to next page
Home, g      : move to top
End, G       : move to bottom
Enter        : chdir or play
Tab          : switch between filelist/playlist
n            : next track
x, Space     : toggle play/pause
Left, Right  : seek backward/forward
/            : search
[, ]         : previous/next search match
Esc          : cancel
0..9         : volume control
h            : help
q, Q         : quit

Filelist
--------
a            : add to playlist
s            : recursive search
BS           : go to parent dir
r            : refresh

Playlist
--------
d, D         : delete item/all
m, M         : move item down/up
r, R         : toggle repeat/random
s, S         : shuffle/sort playlist
w            : enter filename for current playlist
C            : close current playlist
@            : jump to current track

GamePi13
--------
D-pad        : navigate
A            : select/play
B            : go back
X            : play/pause
Y            : next track
L, R         : seek backward/forward
Select       : help
Start        : switch tabs"""

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80
JS_EVENT = struct.Struct('<IhBB')
INPUT_EVENT = struct.Struct('@llHHi')
JSIOCGBTNMAP = 0x84006A34
EV_KEY = 0x01
EV_ABS = 0x03
ABS_X = 0x00
ABS_Y = 0x01
BTN_TO_KEY = {
    0x130: '\n',  # A
    0x131: curses.KEY_BACKSPACE,  # B
    0x133: ' ',  # X
    0x134: 'n',  # Y
    0x136: curses.KEY_LEFT,  # L
    0x137: curses.KEY_RIGHT,  # R
    0x13A: 'h',  # Select
    0x13B: '\t',  # Start
}
GAMEPI13_GPIO_KEYS = {
    5: curses.KEY_UP,
    6: curses.KEY_DOWN,
    16: curses.KEY_LEFT,
    13: curses.KEY_RIGHT,
    26: '\t',
    19: 'h',
    21: '\n',
    20: curses.KEY_BACKSPACE,
    15: ' ',
    12: 'n',
    14: curses.KEY_RIGHT,
    23: curses.KEY_LEFT,
}


def clamp(value, _min, _max):
    return max(_min, min(_max, value))


def space_between(a, b, n):
    d = n - (len(a) + len(b))
    if d >= 0:
        return a + ' ' * d + b
    else:
        return a[:d] + b


def format_time(total):
    h, s = divmod(int(total), 3600)
    m, s = divmod(s, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def str_match(query, s):
    return all(q in s.casefold() for q in query.casefold().split())


def resize(*_args):
    os.write(app.resize_out, b'.')


@functools.cache
def get_mpv_version():
    p = subprocess.run(['mpv', '--version'], stdout=subprocess.PIPE, check=True)
    s = p.stdout.split(b' ', 2)[1].decode().lstrip('v')
    return tuple(int(i) for i in s.split('.'))


@functools.lru_cache
def relpath(path):
    if path.startswith('http'):
        return path
    else:
        return os.path.relpath(path, filelist.path)


@contextmanager
def enable_ctrl_keys():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tcattr = termios.tcgetattr(fd)
        tcattr[0] = tcattr[0] & ~(termios.IXON)
        termios.tcsetattr(fd, termios.TCSANOW, tcattr)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_ext(path):
    return os.path.splitext(path)[1].lstrip('.').lower()


def listdir(path):
    try:
        with os.scandir(path) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if entry.name.startswith('.'):
                    continue
                ext = get_ext(entry.name)
                if entry.is_dir() or ext in AUDIO_EXTENSIONS or ext == 'm3u':
                    yield entry
    except OSError:
        pass


def walk(path, seen=None):
    realpath = os.path.realpath(path)
    seen = seen or set()
    if realpath in seen:
        return []
    seen.add(realpath)

    result = []
    for entry in listdir(path):
        if entry.is_dir():
            children = walk(entry.path, seen)
            if children:
                result.append(entry.path)
                result += children
        else:
            result.append(entry.path)
    return result


class Player:
    def __init__(self):
        self.path = None
        self.position = 0
        self.length = 0
        self.metadata = None
        self._seek_step = 0
        self._seek_timeout = None
        self.is_playing = False
        self._playing = 0
        self._buffer = b''
        self._sid_proc = None
        self._sid_paused = False
        self._sid_started_at = None
        self._sid_finished_at = None
        self._sid_exit_reported = False

        self.socket, self.socket_mpv = socket.socketpair()
        self._proc = subprocess.Popen(
            [
                'mpv',
                f'--input-ipc-client=fd://{self.socket_mpv.fileno()}',
                '--idle',
                '--audio-display=no',
                '--replaygain=track',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=[self.socket_mpv.fileno()],
        )

        self._ipc('observe_property', 1, 'time-pos')
        self._ipc('observe_property', 2, 'duration')
        self._ipc('observe_property', 3, 'metadata')

    def _ipc(self, cmd, *args):
        data = json.dumps({'command': [cmd, *args]})
        msg = data.encode('utf-8') + b'\n'
        self.socket.send(msg)

    def handle_ipc(self, data):
        if data.get('event') == 'property-change' and data['id'] == 1:
            if data.get('data') is not None and not self._seek_step:
                self.position = data['data']
        elif data.get('event') == 'property-change' and data['id'] == 2:
            if data.get('data') is not None:
                self.length = data['data']
        elif data.get('event') == 'property-change' and data['id'] == 3:
            self.metadata = data.get('data')
        elif data.get('event') == 'end-file':
            self._playing -= 1

    def parse_progress(self):
        self._buffer += self.socket.recv(1024)
        msgs = self._buffer.split(b'\n')
        self._buffer = msgs.pop()
        for msg in msgs:
            data = json.loads(msg.decode('utf-8', errors='replace'))
            self.handle_ipc(data)

    def get_progress(self):
        if self._is_sid:
            self._update_sid_position()
        if self.length == 0:
            return 0
        return self.position / self.length

    def get_title(self):
        title = relpath(self.path)
        if self.metadata and 'icy-title' in self.metadata:
            title = '{} [{}]'.format(title, self.metadata['icy-title'])
        return title

    def set_volume(self, vol):
        self._ipc('set', 'volume', str(vol))

    @property
    def _is_sid(self):
        return (
            self.path is not None
            and not self.path.startswith(('http://', 'https://'))
            and get_ext(self.path) == 'sid'
        )

    def _update_sid_position(self):
        if (
            self._sid_proc
            and self._sid_proc.poll() is None
            and self._sid_started_at is not None
        ):
            now = time.monotonic()
            self.position += now - self._sid_started_at
            self._sid_started_at = now

    def _stop_sid(self):
        proc = self._sid_proc
        self._sid_proc = None
        self._sid_paused = False
        self._sid_started_at = None
        if proc is None:
            return
        if proc.poll() is None:
            if SID_DEBUG:
                import traceback
                print('--- _stop_sid() terminating a live process, call stack: ---', file=sys.stderr, flush=True)
                traceback.print_stack(file=sys.stderr)
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _start_sid(self, *, paused=False):
        self._stop_sid()
        self._sid_finished_at = None
        command = [
            'sidplayfp',
            '-q',
            f'-b{self.position % SID_MAX_DURATION:.3f}',
            f'-t{SID_MAX_DURATION}',
            os.path.abspath(self.path),
        ]
        if SID_DEBUG:
            print(
                f'Starting SID process: {command!r}',
                file=sys.stderr,
                flush=True,
            )
        try:
            self._sid_proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None if SID_DEBUG else subprocess.DEVNULL,
            )
            if SID_DEBUG:
                print(f'PID: {self._sid_proc.pid}', file=sys.stderr, flush=True)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            raise

        self._sid_exit_reported = False
        self._sid_started_at = time.monotonic()
        self._sid_paused = False
        self.is_playing = True
        if paused:
            self._sid_proc.send_signal(signal.SIGSTOP)
            self._sid_started_at = None
            self._sid_paused = True
            self.is_playing = False

    def stop(self):
        if self._is_sid:
            self._update_sid_position()
        self.is_playing = False
        self._stop_sid()
        self._ipc('stop')

    def _play(self):
        if not self.path:
            self._stop_sid()
            self.is_playing = False
            return
        if self._is_sid:
            self._ipc('stop')
            self._start_sid()
            return
        self._stop_sid()
        self.is_playing = True
        self._playing += 1
        if get_mpv_version() >= (0, 38, 0):
            self._ipc('loadfile', self.path, 'replace', 0, f'start={self.position}')
        else:
            self._ipc('loadfile', self.path, 'replace', f'start={self.position}')

    def play(self, path):
        if path and (m := re.match(r'^(http.*)#t=([0-9]+)$', path)):
            self.path = m[1]
            self.position = float(m[2])
        else:
            self.path = path
            self.position = 0
        self._sid_finished_at = None
        self.length = 0
        self._seek_step = 0
        self._play()

    def toggle(self):
        if self._is_sid and self._sid_proc and self._sid_proc.poll() is None:
            if self._sid_paused:
                self._sid_proc.send_signal(signal.SIGCONT)
                self._sid_started_at = time.monotonic()
                self._sid_paused = False
                self.is_playing = True
            else:
                self._update_sid_position()
                self._sid_proc.send_signal(signal.SIGSTOP)
                self._sid_started_at = None
                self._sid_paused = True
                self.is_playing = False
            return
        if self.is_playing:
            self.stop()
        elif self.path:
            self._play()

    def seek(self, direction):
        if self._is_sid:
            self._update_sid_position()
            self.position = max(0, self.position + direction * 5)
            self._seek_timeout = time.time() + 0.5
            return
        d = direction * self.length * 0.002
        if self._seek_step * d > 0:  # same direction
            self._seek_step += d
        else:
            self._seek_step = d

        self.position += self._seek_step
        self.position = min(self.length, max(0, self.position))
        self._seek_timeout = time.time() + 0.5

    def finish_seek(self):
        if self._seek_timeout and time.time() >= self._seek_timeout:
            self._seek_timeout = None
            self._seek_step = 0
            if self._is_sid and self._sid_proc:
                self._start_sid(paused=self._sid_paused)
            elif self.is_playing:
                self._play()

    @property
    def is_finished(self):
        if self._is_sid:
            if not self.is_playing:
                return False
            returncode = (
                self._sid_proc.poll() if self._sid_proc is not None else None
            )
            finished = self._sid_proc is not None and returncode is not None
            if not finished:
                return False
            if SID_DEBUG and not self._sid_exit_reported:
                print(
                    f'SID process exited ({returncode}): {self.path}',
                    file=sys.stderr,
                    flush=True,
                )
                self._sid_exit_reported = True
            now = time.monotonic()
            if self._sid_finished_at is None:
                self._sid_finished_at = now
            return now - self._sid_finished_at >= SID_NEXT_TRACK_DELAY
        return self.is_playing and self._playing == 0

    def cleanup(self):
        self._stop_sid()
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self.socket.close()
        self.socket_mpv.close()


class Input:
    def __init__(self):
        self.active = False
        self.str = ''

    def start(self, prompt, on_input=None, on_submit=None, initial=''):
        self.str = initial
        self.prompt = prompt
        self.on_input = on_input
        self.on_submit = on_submit
        self.active = True
        if self.on_input:
            self.on_input(self.str)

    def process_key(self, key):
        if not self.active:
            return False
        if key == chr(27):
            self.str = ''
            self.active = False
        elif key == '\n':
            self.active = False
            if self.on_submit:
                self.on_submit(self.str)
        elif key == curses.KEY_BACKSPACE:
            self.str = self.str[:-1]
        elif isinstance(key, str):
            self.str += key
        else:
            self.active = False
            return False
        if self.on_input:
            self.on_input(self.str)
        return True


class List:
    def __init__(self):
        self.items = []
        self.position = 0
        self.cursor = 0
        self.active = -1
        self.search_str = ''

    @property
    def rows(self):
        return app.rows - 4

    def get_title(self):
        raise NotImplementedError

    def set_cursor(self, cursor):
        self.cursor = clamp(cursor, 0, len(self.items) - 1)
        if len(self.items) < self.rows:
            self.position = 0
        else:
            self.position = clamp(
                self.position, self.cursor - self.rows + 1, self.cursor
            )
            if len(self.items) < self.rows + self.position:
                self.position = len(self.items) - self.rows

    def move_cursor(self, diff):
        self.set_cursor(self.cursor + diff)

    def search(self, q, diff=1, offset=0):
        self.search_str = q
        for i in range(len(self.items)):
            pos = (self.cursor + (i + offset) * diff) % len(self.items)
            if str_match(q, self.format_item(self.items[pos])):
                self.set_cursor(pos)
                return True
        return False

    def format_item(self, item):
        return relpath(item)

    def render(self):
        items = self.items[self.position:self.position + self.rows]
        for i, item in enumerate(items):
            attr = 0
            if self.position + i == self.cursor:
                attr |= curses.A_REVERSE
            if self.position + i == self.active:
                attr |= curses.A_BOLD
            s_item = self.format_item(item)
            s_item = space_between(f'  {s_item}', '', app.cols)
            yield (s_item, attr)
        for _i in range(max(0, self.rows - len(items))):
            yield ''

    def process_key(self, key):  # noqa: C901
        if key in [curses.KEY_DOWN, 'j']:
            self.move_cursor(1)
        elif key in [curses.KEY_UP, 'k']:
            self.move_cursor(-1)
        elif key in [curses.KEY_NPAGE, 'J']:
            self.move_cursor(self.rows - 2)
        elif key in [curses.KEY_PPAGE, 'K']:
            self.move_cursor(-(self.rows - 2))
        elif key in [curses.KEY_END, 'G']:
            self.set_cursor(len(self.items))
        elif key in [curses.KEY_HOME, 'g']:
            self.set_cursor(0)
        elif key == '/':
            app.input.start('/', on_input=self.search)
        elif key == ']':
            if self.search_str:
                self.search(self.search_str, 1, 1)
        elif key == '[':
            if self.search_str:
                self.search(self.search_str, -1, 1)
        else:
            return False
        return True


class HelpList(List):
    def __init__(self):
        super().__init__()
        self.items = HELP.split('\n')

    def get_title(self):
        return 'Help'

    def format_item(self, item):
        return item

    def process_key(self, key):
        if key in ['q', 'h']:
            app.help = False
        else:
            return super().process_key(key)
        return True


class Filelist(List):
    def __init__(self):
        super().__init__()
        self.path = os.getcwd()
        self.all_items = []
        self.items = []
        self.search_cache = []
        self.position = 0
        self.cursor = 0
        self.rsearch_str = ''
        self.set_path(self.path, fail_silently=False)

    def get_title(self):
        title = f'Filelist: {self.path.rstrip("/")}/'
        if self.rsearch_str:
            title += f'search "{self.rsearch_str}"/'
        return title

    def format_item(self, item):
        s = super().format_item(item)
        ext = get_ext(item)
        if not (ext in AUDIO_EXTENSIONS or ext == 'm3u'):
            s += '/'
        return s

    def set_path(self, path, *, prev=None, refresh=False, fail_silently=True):
        if path != self.path:
            try:
                os.chdir(path)
            except Exception:
                if fail_silently:
                    return
                raise
            self.path = path
            relpath.cache_clear()
            self.search_cache = []
        elif refresh:
            self.search_cache = []
        self.all_items = []
        self.rsearch_str = ''

        self.all_items = [entry.path for entry in listdir(path)]
        self.items = self.all_items

        if prev and prev in self.items:
            self.set_cursor(self.items.index(prev))
        else:
            self.position = 0
            self.cursor = 0

    def filter(self, query):
        if not self.search_cache:
            self.search_cache = walk(self.path)

        if query:
            if self.rsearch_str and query.startswith(self.rsearch_str):
                base = self.items
            else:
                base = self.search_cache

            self.items = []
            for path in base:
                if str_match(query, self.format_item(path)):
                    self.items.append(path)
            self.set_cursor(0)
        else:
            self.items = self.all_items

        self.rsearch_str = query

    def activate(self, item):
        ext = get_ext(item)
        if os.path.isdir(item):
            self.set_path(item)
        elif ext in AUDIO_EXTENSIONS:
            playlist.play_from_directory(item)
        elif ext == 'm3u':
            playlist.load(item)
            app.toggle_tabs()

    def process_key(self, key):
        if key == 'a':
            if self.items and playlist.add(self.items[self.cursor]):
                self.move_cursor(1)
        elif key == 's':
            app.input.start('search: ', on_input=self.filter)
            self.filter(self.rsearch_str)
        elif key == '\n':
            if self.items:
                self.activate(self.items[self.cursor])
        elif key == 'r':
            self.set_path(self.path, refresh=True)
        elif key == curses.KEY_BACKSPACE:
            if self.rsearch_str:
                self.set_path(self.path)
            else:
                self.set_path(os.path.dirname(self.path), prev=self.path)
        else:
            return super().process_key(key)
        return True


class Playlist(List):
    def __init__(self):
        super().__init__()
        self.repeat = False
        self.random = False
        self._played = set()
        self.path = None
        self.items_written = []

    def get_title(self):
        title = 'Playlist'
        if self.path:
            title += f' {os.path.basename(self.path)}'
            if self.items != self.items_written:
                title += '*'
        if self.repeat:
            title += ' [repeat all]'
        if self.random:
            title += ' [random]'
        return title

    def clear(self):
        self.items = []
        self.position = 0
        self.cursor = 0
        self.active = -1
        self._played = set()

    def play_from_directory(self, path):
        selected = os.path.abspath(path)
        directory = os.path.dirname(selected)
        self.clear()
        self.path = None
        self.items_written = []
        self.items = [
            entry.path
            for entry in listdir(directory)
            if not entry.is_dir() and get_ext(entry.name) in AUDIO_EXTENSIONS
        ]
        self.active = self.items.index(selected)
        self.set_cursor(self.active)
        player.play(selected)

    def reorder(self, fn):
        if not self.items:
            return
        cursor_item = self.items[self.cursor]
        try:
            active_item = self.items[self.active]
        except IndexError:
            active_item = None
        fn()
        self.set_cursor(self.items.index(cursor_item))
        if active_item:
            self.active = self.items.index(active_item)

    def shuffle(self):
        self.reorder(lambda: random.shuffle(self.items))

    def sort(self):
        self.reorder(lambda: self.items.sort())

    def remove_item(self):
        self.items.pop(self.cursor)

        if self.active == self.cursor:
            self.active = -1
        elif self.active > self.cursor:
            self.active -= 1

        self.move_cursor(0)

    def move_item(self, direction):
        new_cursor = clamp(self.cursor + direction, 0, len(self.items) - 1)

        if self.active == self.cursor:
            self.active = new_cursor
        elif direction < 0:
            if self.active >= new_cursor and self.active < self.cursor:
                self.active += 1
        else:
            if self.active <= new_cursor and self.active > self.cursor:
                self.active -= 1

        item = self.items.pop(self.cursor)
        self.items.insert(new_cursor, item)
        self.set_cursor(new_cursor)

    def next(self):
        if not self.items:
            return

        if self.random:
            self._played.add(self.active)
            left = set(range(len(self.items))).difference(self._played)
            if left:
                self.active = random.choice(list(left))
            else:
                self._played = set()
                if self.repeat:
                    self.active = random.randrange(len(self.items))
                else:
                    self.active = -1
                    return
        else:
            self.active += 1
            if self.active >= len(self.items) and self.repeat:
                self.active = 0
        try:
            return self.items[self.active]
        except IndexError:
            self.active = -1

    def add_dir(self, root):
        count = 0
        for path in walk(root):
            if get_ext(path) in AUDIO_EXTENSIONS:
                count += self.add(path)
        return count

    def add_playlist(self, path):
        count = 0
        dirname = os.path.dirname(path)
        with open(path, errors='replace') as fh:
            for _line in fh:
                line = _line.strip()
                if not line or line[0] == '#':
                    continue
                if not re.match(r'^(/|https?://)', line):
                    line = os.path.join(dirname, line)
                self.items.append(line)
                count += 1
        return count

    def add(self, path):
        ext = get_ext(path)
        if os.path.isdir(path):
            return self.add_dir(path)
        elif ext == 'm3u':
            return self.add_playlist(path)
        elif ext in AUDIO_EXTENSIONS:
            self.items.append(path)
            return 1
        else:
            return 0

    def load(self, path):
        self.clear()
        self.add_playlist(path)
        self.path = path
        self.items_written = self.items.copy()

    def write(self, path):
        try:
            with open(path, 'w') as fh:
                fh.writelines(self.items)
                self.path = path
                self.items_written = self.items.copy()
        except OSError:
            pass

    def process_key(self, key):  # noqa: C901
        if key == 'm':
            self.move_item(1)
        elif key == 'M':
            self.move_item(-1)
        elif key == 'd':
            self.remove_item()
        elif key == 'D':
            self.clear()
        elif key == 'C':
            self.clear()
            self.path = None
            self.items_written = []
        elif key == '\n':
            if not self.items:
                return True
            self.play_from_directory(self.items[self.cursor])
        elif key == '@':
            self.set_cursor(self.active)
        elif key == 's':
            self.shuffle()
        elif key == 'S':
            self.sort()
        elif key == 'r':
            self.repeat = not self.repeat
        elif key == 'R':
            self.random = not self.random
        elif key == 'w':
            app.input.start(
                'write playlist to path: ',
                on_submit=self.write,
                initial=self.path or filelist.path,
            )
        else:
            return super().process_key(key)
        return True


class GamePi13Controller:
    def __init__(self):
        self.fd = None
        self.notify_fd = None
        self.notify_write_fd = None
        self.mode = None
        self.buttons = {}
        self.gpio_buttons = []
        self.gpio_keys = queue.SimpleQueue()
        self.buffer = b''
        self.axes = {0: 0, 1: 0}
        self.repeat_key = None
        self.repeat_at = None
        self.repeat_interval = 0.12
        path = self._find_device()
        if path is None:
            self._init_gpiozero()
            return
        self.mode = 'evdev' if os.path.basename(path).startswith('event') else 'joystick'
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if self.mode == 'evdev':
            return
        button_map = bytearray(512 * 2)
        try:
            fcntl.ioctl(self.fd, JSIOCGBTNMAP, button_map, True)
        except OSError:
            self.close()
            raise
        codes = struct.unpack('<512H', button_map)
        self.buttons = {
            index: BTN_TO_KEY[code]
            for index, code in enumerate(codes)
            if code in BTN_TO_KEY
        }

    @staticmethod
    def _device_name(path):
        basename = os.path.basename(path)
        name_path = f'/sys/class/input/{basename}/device/name'
        try:
            with open(name_path) as name_file:
                return name_file.read().strip()
        except OSError:
            return ''

    @classmethod
    def _find_device(cls):
        configured = os.getenv('CPLAY_GAMEPI13_DEVICE')
        if configured:
            return configured

        joystick_paths = sorted(glob.glob('/dev/input/js[0-9]*'))
        event_paths = sorted(glob.glob('/dev/input/event[0-9]*'))
        for path in joystick_paths + event_paths:
            if 'gpio controller' in cls._device_name(path).casefold():
                return path
        return None

    def _init_gpiozero(self):
        try:
            with open('/proc/device-tree/model', 'rb') as model_file:
                is_raspberry_pi = b'raspberry pi' in model_file.read().lower()
        except OSError:
            return
        if not is_raspberry_pi:
            return

        try:
            from gpiozero import Button
        except ImportError as error:
            raise RuntimeError(
                'GamePi13 GPIO controls require gpiozero; '
                'install it with "sudo apt install python3-gpiozero"'
            ) from error

        with ExitStack() as resources:
            self.notify_fd, self.notify_write_fd = os.pipe2(os.O_NONBLOCK)
            resources.callback(os.close, self.notify_fd)
            resources.callback(os.close, self.notify_write_fd)
            for pin, key in GAMEPI13_GPIO_KEYS.items():
                button = Button(pin, pull_up=True, bounce_time=0.05)
                resources.callback(button.close)
                button.when_pressed = lambda key=key: self._queue_gpio_key(key)
                self.gpio_buttons.append(button)
            resources.pop_all()
        self.mode = 'gpiozero'

    def _queue_gpio_key(self, key):
        self.gpio_keys.put(key)
        try:
            os.write(self.notify_write_fd, b'.')
        except BlockingIOError:
            pass

    def process_gpio_events(self, process_key):
        if self.notify_fd is None:
            return
        while True:
            try:
                if not os.read(self.notify_fd, 4096):
                    break
            except BlockingIOError:
                break
        while True:
            try:
                key = self.gpio_keys.get_nowait()
            except queue.Empty:
                break
            process_key(key)

    def _set_axis(self, number, value, process_key):
        if number not in self.axes:
            return
        old_direction = self.axes[number]
        threshold = 0.5 if self.mode == 'evdev' else 16000
        direction = -1 if value < -threshold else 1 if value > threshold else 0
        if self.axes[number] == direction:
            return
        self.axes[number] = direction
        if old_direction:
            old_key = (
                (curses.KEY_LEFT if old_direction < 0 else curses.KEY_RIGHT)
                if number == 0
                else (curses.KEY_UP if old_direction < 0 else curses.KEY_DOWN)
            )
            if self.repeat_key == old_key:
                self.repeat_key = None
                self.repeat_at = None
        if not direction:
            return
        if number == 0:
            key = curses.KEY_LEFT if direction < 0 else curses.KEY_RIGHT
        else:
            key = curses.KEY_UP if direction < 0 else curses.KEY_DOWN

        self.repeat_key = key
        process_key(key)
        self.repeat_at = time.monotonic() + 0.45

    def process_events(self, process_key):
        try:
            event_struct = INPUT_EVENT if self.mode == 'evdev' else JS_EVENT
            data = os.read(self.fd, event_struct.size * 32)
        except BlockingIOError:
            return
        if not data:
            self.close()
            return
        self.buffer += data
        while len(self.buffer) >= event_struct.size:
            if self.mode == 'evdev':
                _seconds, _microseconds, event_type, number, value = (
                    INPUT_EVENT.unpack_from(self.buffer)
                )
                if event_type == EV_ABS and number in (ABS_X, ABS_Y):
                    self._set_axis(number, value, process_key)
                elif event_type == EV_KEY and value == 1:
                    key = BTN_TO_KEY.get(number)
                    if key is not None:
                        process_key(key)
            else:
                _, value, event_type, number = JS_EVENT.unpack_from(self.buffer)
                if not event_type & JS_EVENT_INIT:
                    if event_type == JS_EVENT_AXIS:
                        self._set_axis(number, value, process_key)
                    elif event_type == JS_EVENT_BUTTON and value:
                        key = self.buttons.get(number)
                        if key is not None:
                            process_key(key)
            self.buffer = self.buffer[event_struct.size:]

    def get_timeout(self):
        if self.repeat_at is None:
            return None
        return max(0, self.repeat_at - time.monotonic())

    def repeat(self, process_key):
        if self.repeat_at is None or time.monotonic() < self.repeat_at:
            return
        if self.repeat_key is None:
            self.repeat_at = None
            return
        process_key(self.repeat_key)
        self.repeat_at = time.monotonic() + self.repeat_interval

    def close(self):
        for button in self.gpio_buttons:
            button.close()
        self.gpio_buttons.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.notify_fd is not None:
            os.close(self.notify_fd)
            self.notify_fd = None
        if self.notify_write_fd is not None:
            os.close(self.notify_write_fd)
            self.notify_write_fd = None


class Application:
    def __init__(self):
        self.tabs = [filelist, playlist]
        self.help = False
        self.input = Input()
        self.old_lines = []
        self.screen: curses.window
        self.controller = GamePi13Controller()

        # self-pipe to avoid concurrency issues with signal
        self.resize_in, self.resize_out = os.pipe2(os.O_NONBLOCK)

    def refresh_dimensions(self):
        self.rows, self.cols = self.screen.getmaxyx()

    def on_resize(self):
        curses.endwin()
        self.screen.refresh()
        self.refresh_dimensions()
        self.tab.set_cursor(app.tab.cursor)

    @property
    def tab(self):
        if self.help:
            return helplist
        else:
            return self.tabs[0]

    def toggle_tabs(self):
        self.tabs.append(self.tabs.pop(0))

    def format_progress(self):
        progress = min(int(self.cols * player.get_progress()), self.cols - 1)
        return '=' * (progress - 1) + '|' + '-' * (self.cols - progress)

    def _render(self):
        yield (self.tab.get_title(), curses.A_BOLD)
        yield '-' * self.cols

        yield from self.tab.render()
        yield self.format_progress()

        if self.input.active:
            status = f'{self.input.prompt}{self.input.str}█'
        elif self.tab == helplist:
            status = f'cplay-ng {__version__}'
        elif player.is_playing:
            status = f'Playing: {player.get_title()}'
        else:
            status = ''

        counter = ' / '.join([
            format_time(player.position),
            format_time(player.length),
        ])
        yield space_between(status, counter, self.cols)

    def render(self, *, force=False):
        lines = list(self._render())
        try:
            for i, line in enumerate(lines):
                if (
                    not force
                    and len(self.old_lines) > i
                    and line == self.old_lines[i]
                ):
                    continue
                self.screen.move(i, 0)
                self.screen.clrtoeol()
                if isinstance(line, str):
                    self.screen.insstr(line, 0)
                else:
                    self.screen.insstr(*line)
            # make sure cursor is in a meaningful position for a11y
            self.screen.move(self.tab.cursor - self.tab.position + 2, 0)
            self.screen.refresh()
        except curses.error:
            pass
        self.old_lines = lines

    def process_key(self, key):  # noqa: C901
        if self.input.process_key(key):  # noqa: SIM114
            pass
        elif self.tab.process_key(key):
            pass
        elif key in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']:
            player.set_volume(int(key, 10) * 11)
        elif key == curses.KEY_RIGHT:
            player.seek(1)
        elif key == curses.KEY_LEFT:
            player.seek(-1)
        elif key in ['x', ' ']:
            player.toggle()
        elif key == 'n':
            player.play(playlist.next())
        elif key == 'h':
            self.help = True
        elif key in ['q', 'Q']:
            sys.exit(0)
        elif key == '\t':
            app.toggle_tabs()
        else:
            return False
        return True

    def run(self):
        self.refresh_dimensions()
        self.render()

        with selectors.DefaultSelector() as sel:
            sel.register(sys.stdin, selectors.EVENT_READ)
            sel.register(self.resize_in, selectors.EVENT_READ)
            sel.register(player.socket, selectors.EVENT_READ)
            if self.controller.fd is not None:
                sel.register(self.controller.fd, selectors.EVENT_READ)
            if self.controller.notify_fd is not None:
                sel.register(self.controller.notify_fd, selectors.EVENT_READ)
            prev = time.time()

            while True:
                player.finish_seek()

                timeout = 0.5 if player.is_playing else None
                controller_timeout = self.controller.get_timeout()
                if controller_timeout is not None:
                    timeout = (
                        controller_timeout
                        if timeout is None
                        else min(timeout, controller_timeout)
                    )
                for key, _mask in sel.select(timeout):
                    # if we have skipped multiple seconds, it is probably
                    # because the system was suspended. This heuristic is much
                    # simpler than detecting suspend via dbus.
                    #if player.is_playing and time.time() - prev > 5:
                       # player.stop()
                    #prev = time.time()
                    #previous lines had bug

                    if key.fileobj is self.resize_in:
                        os.read(self.resize_in, 8)
                        self.on_resize()
                        self.render(force=True)
                    elif key.fileobj is sys.stdin:
                        self.process_key(self.screen.get_wch())
                    elif key.fileobj is player.socket:
                        player.parse_progress()
                    elif key.fileobj == self.controller.notify_fd:
                        self.controller.process_gpio_events(self.process_key)
                    elif key.fileobj == self.controller.fd:
                        controller_fd = key.fileobj
                        self.controller.process_events(self.process_key)
                        if self.controller.fd is None:
                            sel.unregister(controller_fd)

                self.controller.repeat(self.process_key)
                if player.is_finished:
                    next_path = playlist.next()
                    if SID_DEBUG:
                        print(
                            f'Advancing playlist to: {next_path!r}',
                            file=sys.stderr,
                            flush=True,
                        )
                    player.play(next_path)

                self.render()


player = Player()
playlist = Playlist()
filelist = Filelist()
helplist = HelpList()
app = Application()


def main():
    import signal as _s
    print(f'SIGCHLD handler: {_s.getsignal(_s.SIGCHLD)}', file=sys.stderr)
    app.screen = curses.initscr()
    app.screen.keypad(True)  # noqa: FBT003
    curses.cbreak()
    curses.noecho()
    curses.meta(True)  # noqa: FBT003
    curses.curs_set(0)

    signal.signal(signal.SIGWINCH, resize)

    try:
        with enable_ctrl_keys():
            app.run()
    finally:
        app.controller.close()
        player.cleanup()
        curses.endwin()


if __name__ == '__main__':
    main()
