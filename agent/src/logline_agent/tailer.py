'''
File discovery for the agent: resolve the configured globs into the concrete
set of files that should be watched right now.
'''

from glob import glob
from hashlib import sha256
from pathlib import Path


def iter_log_files(conf):
    paths = set()
    for glob_str in conf.scan_globs:
        for p in glob(glob_str, recursive=True):
            parent = Path(p).parent
            if any((parent / filename).exists() for filename in conf.exclude_if_file_present):
                continue
            paths.add(Path(p).resolve())
    for glob_str in conf.exclude_globs:
        for p in glob(glob_str, recursive=True):
            paths.discard(Path(p).resolve())
    return sorted(paths)


def sha256_hex(data):
    return sha256(data).hexdigest()
