from os import chdir
from pathlib import Path

from conftest import dst_path, free_port, run_agent, run_server, wait_for_bytes


def test_multiple_files_over_one_connection(tmp_path):
    chdir(tmp_path)
    src = Path('src')
    src.mkdir()
    dst = Path('dst')
    dst.mkdir()
    files = {f'app{i}.log': (f'content of file {i}\n' * 4).encode() for i in range(6)}
    for name, data in files.items():
        (src / name).write_bytes(data)
    port = free_port()
    with run_server(dst, port), run_agent(f'{src}/*.log', port):
        # All files are multiplexed over the single agent->server connection.
        for name, data in files.items():
            wait_for_bytes(dst_path(dst, src, name), data, timeout=10)


def test_large_file_streams_completely(tmp_path):
    chdir(tmp_path)
    src = Path('src')
    src.mkdir()
    dst = Path('dst')
    dst.mkdir()
    # Comfortably larger than the default 4 MiB in-flight window, so the
    # transfer must pipeline and respect flow control rather than send in one go.
    data = b''.join(f'{i:09d} the quick brown fox jumps over the lazy dog\n'.encode() for i in range(120_000))
    assert len(data) > 6 * 1024 * 1024
    (src / 'big.log').write_bytes(data)
    port = free_port()
    with run_server(dst, port), run_agent(f'{src}/*.log', port):
        wait_for_bytes(dst_path(dst, src, 'big.log'), data, timeout=30)
