'''
Safe construction of destination file paths from untrusted client input.

The hostname and path in an OPEN frame come from an authenticated but
otherwise untrusted agent, so they must never be allowed to escape the
configured destination directory via path traversal.
'''

from reprlib import repr as smart_repr

from .framing import ProtocolError


def _is_safe_path_segment(segment):
    '''
    A safe path segment is a non-empty string that refers to a single
    directory/file entry and cannot be used to traverse the filesystem.
    '''
    if not segment or segment in ('.', '..'):
        return False
    if '/' in segment or '\\' in segment or '\x00' in segment:
        return False
    return True


def build_destination_path(destination_directory, hostname, path):
    '''
    Build the destination file path for a received log file, keyed on the
    client hostname and the source path.
    '''
    if not _is_safe_path_segment(hostname):
        raise ProtocolError(f'Invalid hostname: {smart_repr(hostname)}')

    *dir_parts, filename = path.strip('/').split('/')
    if not _is_safe_path_segment(filename):
        raise ProtocolError(f'Invalid path: {smart_repr(path)}')

    # dir_parts are joined with '~' into a single path segment, so any '/' they
    # might contain has already been removed by split('/'); still reject any
    # remaining traversal or null-byte characters defensively.
    mangled_dir = '~'.join(dir_parts)
    if '\x00' in mangled_dir or mangled_dir in ('.', '..'):
        raise ProtocolError(f'Invalid path: {smart_repr(path)}')

    base = destination_directory.resolve()
    dst_path = (base / hostname / mangled_dir / filename) if mangled_dir \
        else (base / hostname / filename)

    # Final defense in depth: make sure the resolved destination really stays
    # inside the configured destination directory.
    resolved_parent = dst_path.parent.resolve()
    if resolved_parent != base and base not in resolved_parent.parents:
        raise ProtocolError(f'Refusing to write outside destination directory: {smart_repr(str(dst_path))}')

    return dst_path
