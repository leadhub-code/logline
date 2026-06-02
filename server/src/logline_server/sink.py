'''
Per-stream destination file writer with idempotent, offset-based appends.

A Sink owns one destination log file. Writes carry an absolute offset; the
sink appends only the bytes beyond what it already has and silently drops
anything it has already stored, which makes resend-after-reconnect idempotent.
A write whose offset is beyond the current end of file is a gap and rejected.
'''

from datetime import datetime, timezone
from hashlib import sha256
from logging import getLogger
from os import fstat, fsync

from .framing import ProtocolError


logger = getLogger(__name__)


def sha256_hex(data):
    return sha256(data).hexdigest()


def open_sink(dst_path, prefix_size, prefix_sha256, fsync_each_flush):
    '''
    Open (or create) the destination file for a stream, rotating it aside if
    the file that already exists has a different prefix (i.e. the source was
    rotated). Returns a ready-to-use Sink positioned at the end of the file.
    '''
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    if dst_path.is_file():
        with dst_path.open('rb') as existing:
            existing_prefix = existing.read(prefix_size)
        if existing_prefix and sha256_hex(existing_prefix) == prefix_sha256:
            logger.info('Resuming existing file: %s', dst_path)
        else:
            iso_dt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            rotated = dst_path.with_name(f'{dst_path.name}.rotated-{iso_dt}')
            logger.info('Prefix changed, rotating %s -> %s', dst_path, rotated.name)
            dst_path.rename(rotated)

    f = dst_path.open('ab')
    length = fstat(f.fileno()).st_size
    logger.info('Opened sink %s at offset %d', dst_path, length)
    return Sink(dst_path, f, length, fsync_each_flush)


class Sink:

    def __init__(self, dst_path, f, length, fsync_each_flush):
        self.dst_path = dst_path
        self._f = f
        self._length = length
        self._fsync_each_flush = fsync_each_flush

    @property
    def offset(self):
        '''The current end of the file: everything below this is stored.'''
        return self._length

    def write(self, offset, data):
        '''
        Append the part of data that lies beyond the current end of file, and
        flush it out of the process buffer so a reader sees it immediately.
        Returns the number of bytes actually written.
        '''
        if offset > self._length:
            raise ProtocolError(f'Gap in stream for {self.dst_path}: got offset {offset}, expected at most {self._length}')
        end = offset + len(data)
        if end <= self._length:
            return 0  # fully duplicate, already stored
        skip = self._length - offset
        written = self._f.write(data[skip:])
        self._f.flush()
        self._length = end
        return written

    def sync(self):
        '''Force the data durably to disk, if fsync is enabled.'''
        if self._fsync_each_flush:
            fsync(self._f.fileno())

    def close(self):
        try:
            self._f.flush()
            self.sync()
        finally:
            self._f.close()
