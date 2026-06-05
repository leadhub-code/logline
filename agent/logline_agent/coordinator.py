'''
Per-tailed-path coordinator with agent-driven rotation.

The agent is the sole authority on file identity and rotation. For each tailed
path it owns one or more *segment* connections, each bound to an explicit
agent-chosen ``target`` filename on the server:

- In steady state there is exactly one segment, the **live** one, streaming the
  current inode into ``<basename>`` (e.g. ``foo.log``).
- Across a rotation there are briefly two, targeting **distinct** names: the
  closing segment draining the old inode into ``foo.log.<iso_dt>`` and the new
  live segment streaming the new inode into ``foo.log``. They never share a
  name, so the server has nothing to arbitrate.

The one ordering constraint -- seal the old segment before opening the new live
one -- is enforced inside this single process: the coordinator awaits the seal
rename's ack before it starts the new live segment.
'''

import asyncio
from asyncio import create_task
from datetime import datetime, timezone
from logging import getLogger
from os import fstat

from .client import sha1_b64
from .marker_watcher import MarkerWatcher
from .telemetry import record_bytes_read, record_rotation, register_lag_source, unregister_lag_source


logger = getLogger(__name__)

CHUNK_SIZE = 2 ** 20


def utc_iso_dt():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


class Segment:
    '''
    One server connection bound to one inode and one explicit ``target`` name.

    Lifecycle: born **live** (draining the current inode into ``basename``);
    when the coordinator seals it, it renames itself to ``seal_name`` and becomes
    **closing**, draining the now-old inode to completion before a final rename
    (lh-logrotate ``<sha13>``) or a plain close (orphan).
    '''

    def __init__(self, conf, server, file_path, file_stream, inode, target, markers,
                 role_live, is_own_log=False):
        self.conf = conf
        self.server = server
        self.file_path = file_path
        self.file_stream = file_stream
        self.inode = inode
        self.target = target
        self.markers = markers
        self.role_live = role_live
        self.is_own_log = is_own_log

        self.conn = None
        self._pending_seal = None        # (seal_name, is_orphan, iso_dt) once requested
        self._sealed = asyncio.Event()   # set after the seal rename is acked
        self.task = None
        self._lag = 0                    # bytes readable but not yet shipped

    # -- coordinator-facing API --------------------------------------------

    def start(self):
        self.task = create_task(self.run())
        return self.task

    def request_seal(self, seal_name, is_orphan, iso_dt):
        self._pending_seal = (seal_name, is_orphan, iso_dt)

    async def wait_sealed(self):
        # Resolve when the seal rename is acked, or bail if the segment task died
        # first (e.g. a persistent connection failure) so the coordinator is not
        # blocked forever waiting to open the next live segment.
        while not self._sealed.is_set():
            if self.task is not None and self.task.done():
                return
            await asyncio.sleep(self.conf.tail_read_interval_seconds)

    # -- lifecycle ---------------------------------------------------------

    async def run(self):
        register_lag_source(id(self), lambda: self._lag)
        try:
            await self._connect()
            await self._live_phase()
            await self._do_seal()
            await self._closing_phase()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception('Segment %s (target %s) failed: %r', self.file_path, self.target, e)
        finally:
            self._close()

    async def _live_phase(self):
        while self._pending_seal is None:
            sent = await self._drain_available()
            if self._pending_seal is not None:
                break
            if self.is_own_log:
                # do not process our own logfile too often to avoid feedback noise
                await asyncio.sleep(60)
            elif not sent:
                await asyncio.sleep(self.conf.tail_read_interval_seconds)

    async def _do_seal(self):
        seal_name, is_orphan, iso_dt = self._pending_seal
        # flush whatever is already readable before relabelling
        await self._drain_available()
        await self._rename(self.target, seal_name)
        logger.info('Sealed %s as %s', self.file_path, seal_name)
        self.target = seal_name
        self.role_live = False
        self._sealed.set()

    async def _closing_phase(self):
        seal_name, is_orphan, iso_dt = self._pending_seal
        if is_orphan:
            await self._drain_until_idle()
            logger.info('Closing orphan segment %s (target %s)', self.file_path, self.target)
        else:
            sha13 = await self._drain_until_sha13(iso_dt)
            await self._drain_available()  # final flush now that the segment is complete
            final = '{}.{}'.format(seal_name, sha13)
            await self._rename(seal_name, final)
            self.target = final
            logger.info('Finalized %s as %s', self.file_path, final)

    async def _drain_until_idle(self):
        '''Orphan completion: drain until no growth for ``seal_idle_seconds``.'''
        from time import monotonic as monotime
        last_active = monotime()
        while True:
            if await self._drain_available():
                last_active = monotime()
            elif monotime() - last_active > self.conf.seal_idle_seconds:
                return
            await asyncio.sleep(self.conf.tail_read_interval_seconds)

    async def _drain_until_sha13(self, iso_dt):
        '''
        lh-logrotate completion: keep draining trailing bytes until the
        ``.lh-logrotate-{compressed,uploaded}`` marker yields ``<sha13>``.
        '''
        while True:
            await self._drain_available()
            sha13 = self.markers.sha13_for(iso_dt)
            if sha13 is not None:
                return sha13
            await asyncio.sleep(self.conf.tail_read_interval_seconds)

    # -- connection / IO ---------------------------------------------------

    async def _connect(self):
        prefix = self._read_prefix()
        self.conn = await self.server(
            log_path=self.file_path, target=self.target, log_prefix=prefix)
        reply = self.conn.header_reply
        server_length = reply['length']
        if (self.role_live and server_length > 0
                and reply.get('prefix_sha1') not in (None, sha1_b64(prefix))):
            await self._seal_stale_remote(prefix, server_length)
        self.file_stream.seek(self.conn.header_reply['length'])

    async def _seal_stale_remote(self, prefix, server_length):
        '''
        Recovery: the live target already holds a different, completed segment
        (e.g. a rotation we missed across a crash). We must not append to it.
        We have no source for its trailing bytes, so seal it aside as an orphan,
        then reconnect to the now-free live target and stream our inode.
        '''
        seal_name = '{}.{}.orphan'.format(self.target, utc_iso_dt())
        logger.info('Recovery: live target %s holds a different file (len %d); sealing as %s',
                    self.target, server_length, seal_name)
        # This connection's server-side fd follows the rename onto the sealed
        # file; we drop it and reconnect fresh to the original target.
        await self.conn.send_rename(self.target, seal_name)
        self.conn.close()
        self.conn = await self.server(
            log_path=self.file_path, target=self.target, log_prefix=prefix)

    async def _reconnect(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        prefix = self._read_prefix()
        self.conn = await self.server(
            log_path=self.file_path, target=self.target, log_prefix=prefix)

    async def _drain_available(self):
        '''Send all currently-readable bytes; return True if anything was sent.'''
        sent_any = False
        while True:
            pos = self.file_stream.tell()
            chunk = self.file_stream.read(CHUNK_SIZE)
            if not chunk:
                self._lag = 0
                return sent_any
            try:
                await self.conn.send_data(pos, chunk)
            except Exception as e:
                logger.warning('send_data on %s (target %s) failed: %r; reconnecting',
                               self.file_path, self.target, e)
                await self._reconnect()
                self.file_stream.seek(self.conn.header_reply['length'])
                return sent_any
            # Count only after a successful send: a failed send seeks back and
            # re-reads these bytes, so counting post-send avoids double-counting.
            record_bytes_read(len(chunk))
            sent_any = True
            self._lag = self._stream_lag()

    async def _rename(self, src, dst):
        while True:
            try:
                await self.conn.send_rename(src, dst)
                return
            except Exception as e:
                logger.warning('rename %s -> %s on %s failed: %r; reconnecting',
                               src, dst, self.file_path, e)
                # Reconnect targeting the destination: the rename may already be
                # applied server-side (it is idempotent), or still pending.
                self.target = dst
                await self._reconnect()
                if self.conn.header_reply['length'] >= 0:
                    # server now holds `dst`; the rename is effectively done
                    return

    def _read_prefix(self):
        self.file_stream.seek(0)
        return self.file_stream.read(self.conf.prefix_length_bytes)

    def _stream_lag(self):
        '''Bytes appended to the inode but not yet read by this segment.'''
        try:
            return max(0, fstat(self.file_stream.fileno()).st_size - self.file_stream.tell())
        except OSError:
            return 0

    def _close(self):
        unregister_lag_source(id(self))
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        try:
            self.file_stream.close()
        except Exception:
            pass


class PathCoordinator:
    '''Owns all segment connections for a single tailed path.'''

    def __init__(self, conf, file_path, server, is_own_log=False):
        self.conf = conf
        self.file_path = file_path
        self.basename = file_path.name
        self.server = server
        self.is_own_log = is_own_log
        self.markers = MarkerWatcher(conf, file_path.parent, file_path.name)
        self.live = None
        self.closing = set()
        self.consumed_isos = set()
        self.last_inode = None

    async def run(self):
        marker_task = create_task(self.markers.run())
        try:
            while True:
                self._reap_closing()
                inode = self._stat_inode()
                if inode is not None and inode != self.last_inode:
                    await self._on_new_inode(inode)
                    self.last_inode = inode
                await asyncio.sleep(self.conf.tail_read_interval_seconds)
        finally:
            marker_task.cancel()
            await _cancel(marker_task)
            for seg in [self.live, *self.closing]:
                if seg is not None and seg.task is not None:
                    seg.task.cancel()
                    await _cancel(seg.task)

    async def _on_new_inode(self, inode):
        f = self.file_path.open('rb')
        f_inode = fstat(f.fileno()).st_ino
        if f_inode == self.last_inode:
            f.close()
            return
        if self.live is None:
            logger.info('Detected file: %s (inode %s)', self.file_path, f_inode)
        else:
            logger.info('File rotated: %s (inode %s -> %s)', self.file_path, self.last_inode, f_inode)
            record_rotation()
            await self._seal_live()
        self.live = self._make_segment(f, f_inode, target=self.basename, role_live=True)
        self.live.start()

    async def _seal_live(self):
        '''Seal the current live segment and move it to the closing set.'''
        iso_dt, is_orphan = await self._learn_iso_dt()
        seal_name = '{}.{}'.format(self.basename, iso_dt)
        if is_orphan:
            seal_name += '.orphan'
        self.live.request_seal(seal_name, is_orphan, iso_dt)
        await self.live.wait_sealed()   # ordering: seal acked before new live opens
        self.consumed_isos.add(iso_dt)
        self.closing.add(self.live)
        self.live = None

    async def _learn_iso_dt(self):
        '''
        Wait up to ``seal_marker_grace_seconds`` for lh-logrotate's ``.waiting`` marker
        to learn ``<iso_dt>``. If it never appears, fall back to an orphan seal
        with a self-generated timestamp.
        '''
        deadline_steps = max(1, int(self.conf.seal_marker_grace_seconds / self.conf.tail_read_interval_seconds))
        for _ in range(deadline_steps + 1):
            iso_dt = self.markers.newest_unconsumed_waiting(self.consumed_isos)
            if iso_dt is not None:
                return iso_dt, False
            await asyncio.sleep(self.conf.tail_read_interval_seconds)
        return utc_iso_dt(), True

    def _make_segment(self, f, inode, target, role_live):
        return Segment(
            self.conf, self.server, self.file_path, f, inode, target,
            self.markers, role_live, is_own_log=self.is_own_log)

    def _reap_closing(self):
        done = {s for s in self.closing if s.task is not None and s.task.done()}
        self.closing -= done

    def _stat_inode(self):
        try:
            return self.file_path.stat().st_ino
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.debug('Could not stat %s: %r', self.file_path, e)
            return None


async def _cancel(task):
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
