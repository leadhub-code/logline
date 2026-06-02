'''
Per-parent-directory watcher for lh-logrotate marker files.

lh-logrotate exposes both ``<iso_dt>`` and ``<sha13>`` in its own on-disk
filenames as a segment moves through its lifecycle. The agent parrots those
values -- it never invents them -- so the mirror name matches byte-for-byte:

    foo.log.<iso_dt>.lh-logrotate-waiting                       -> iso_dt
    foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-compressed     -> iso_dt, sha13
    foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-uploaded       -> iso_dt, sha13  (fallback)

The watcher periodically scans the directory (the tail cadence) and exposes the
two facts the coordinator must not compute itself.
'''

import asyncio
from logging import getLogger
from os import listdir
import re


logger = getLogger(__name__)

ISO_DT = r'\d{8}T\d{6}Z'
SHA13 = r'[0-9a-f]{13}'


class MarkerWatcher:

    def __init__(self, conf, directory, basename):
        self.conf = conf
        self.directory = directory
        self.basename = basename
        prefix = re.escape(basename) + r'\.'
        self.waiting_re = re.compile(
            '^' + prefix + r'(' + ISO_DT + r')\.lh-logrotate-waiting$')
        self.done_re = re.compile(
            '^' + prefix + r'(' + ISO_DT + r')\.(' + SHA13 + r')'
            r'\.xz\.gpg\.lh-logrotate-(?:compressed|uploaded)$')
        self.waiting_isos = []     # iso_dt seen with a .waiting marker, oldest first
        self.sha_by_iso = {}       # iso_dt -> sha13

    async def run(self):
        while True:
            self.scan()
            await asyncio.sleep(self.conf.tail_read_interval)

    def scan(self):
        try:
            names = listdir(self.directory)
        except OSError as e:
            logger.debug('Could not list %s: %r', self.directory, e)
            return
        for name in names:
            m = self.waiting_re.match(name)
            if m and m.group(1) not in self.waiting_isos:
                self.waiting_isos.append(m.group(1))
            m = self.done_re.match(name)
            if m:
                self.sha_by_iso.setdefault(m.group(1), m.group(2))

    def newest_unconsumed_waiting(self, consumed):
        '''Most recent ``<iso_dt>`` from a ``.waiting`` marker not yet sealed.'''
        self.scan()
        for iso in reversed(self.waiting_isos):
            if iso not in consumed:
                return iso
        return None

    def sha13_for(self, iso_dt):
        '''``<sha13>`` for a sealed segment once its done-marker is on disk.'''
        if iso_dt not in self.sha_by_iso:
            self.scan()
        return self.sha_by_iso.get(iso_dt)
