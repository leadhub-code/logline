import asyncio
from types import SimpleNamespace

from pytest import fixture, mark, raises

from logline_agent import main
from logline_agent.configuration import Configuration
from logline_agent.main import get_argument_parser, iter_files


@fixture
def load_conf(temp_dir):
    def load_conf(conf_yaml):
        (temp_dir / 'configuration.yaml').write_text(conf_yaml)
        args = get_argument_parser().parse_args(['--conf', str(temp_dir / 'configuration.yaml')])
        conf = Configuration(args=args)
        return conf
    return load_conf


def test_iter_files(temp_dir, load_conf):
    conf = load_conf(f'''\
        server: 127.0.0.1:9999
        client_token: topsecret
        scan:
          - {temp_dir}/*/*.log
        exclude:
          - {temp_dir}/excluded/not_this.log
        exclude_if_file_present:
          - skip
    ''')
    assert list(iter_files(conf)) == []
    (temp_dir / 'log').mkdir()
    (temp_dir / 'log' / 'example.log').write_text('Hello World!\n')
    (temp_dir / 'excluded').mkdir()
    (temp_dir / 'excluded' / 'not_this.log').write_text('This file should be excluded\n')
    (temp_dir / 'file-excluded').mkdir()
    (temp_dir / 'file-excluded' / 'skip').write_text('')
    (temp_dir / 'file-excluded' / 'not_this.log').write_text('This file should be also excluded\n')
    assert list(iter_files(conf)) == [(temp_dir / 'log' / 'example.log')]


@mark.parametrize('excluded_before_scan', [True, False])
def test_iter_files_excludes_file_rotated_during_scan(temp_dir, monkeypatch, excluded_before_scan):
    log_path = temp_dir / 'logline-agent' / 'log' / 'logline_agent.log'
    scan_pattern = str(temp_dir / '*' / 'log' / '*.log')
    exclude_pattern = str(temp_dir / 'logline-agent' / 'log' / '*')
    scan_started = False

    def rotating_glob(pattern, recursive):
        nonlocal scan_started
        assert recursive is True
        if pattern == scan_pattern:
            scan_started = True
            return [str(log_path)]
        assert pattern == exclude_pattern
        excluded = excluded_before_scan if not scan_started else not excluded_before_scan
        return [str(log_path)] if excluded else []

    monkeypatch.setattr(main, 'glob', rotating_glob)
    conf = type('Conf', (), {
        'scan_globs': [scan_pattern],
        'exclude_globs': [exclude_pattern],
        'exclude_if_file_present': [],
    })()

    assert list(iter_files(conf)) == []


def test_finished_path_is_pruned_and_can_be_readded(monkeypatch, temp_dir):
    # A coordinator ends on its own once its path is gone; its entry must leave
    # the watched set (and the files_watched gauge), and the path must be
    # watched again if it reappears.
    path = temp_dir / 'app.log'
    scans = [[path], [], [path]]
    started = []
    watched_counts = []
    gauge = {}

    class Stop(Exception):
        pass

    async def fake_watch_path(conf, file_path, client_factory):
        started.append(file_path)

    async def fake_sleep(seconds):
        for _ in range(5):
            await asyncio.sleep(0)  # let the watch task run to completion
        watched_counts.append(gauge['source']())
        if not scans:
            raise Stop()

    monkeypatch.setattr(main, 'watch_path', fake_watch_path)
    monkeypatch.setattr(main, 'iter_files', lambda conf: scans.pop(0))
    monkeypatch.setattr(main, 'sleep', fake_sleep)
    monkeypatch.setattr(main, 'init_metrics', lambda conf: None)
    monkeypatch.setattr(main, 'shutdown_metrics', lambda provider: None)
    monkeypatch.setattr(main, 'set_files_watched_source', lambda source: gauge.setdefault('source', source))
    conf = SimpleNamespace(server_host='127.0.0.1', server_port=1, scan_new_files_interval_seconds=0)

    with raises(Stop):
        asyncio.run(main.async_main(conf))
    assert started == [path, path]
    assert watched_counts == [1, 0, 1]
