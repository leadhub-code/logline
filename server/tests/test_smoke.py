from subprocess import DEVNULL, check_call
import sys


def test_import():
    import logline_server
    assert logline_server


def test_run_server_help():
    check_call([sys.executable, '-m', 'logline_server', '--help'], stdout=DEVNULL)
