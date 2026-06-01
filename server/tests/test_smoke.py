import os


def test_import():
    import logline_server


def test_resolve_workers():
    from logline_server.configuration import resolve_workers
    cpus = os.cpu_count() or 1
    assert resolve_workers(None) == 1
    assert resolve_workers('auto') == cpus
    assert resolve_workers('AUTO') == cpus
    assert resolve_workers('0') == cpus
    assert resolve_workers(0) == cpus
    assert resolve_workers('4') == 4
    assert resolve_workers(4) == 4
    assert resolve_workers('-2') == 1
