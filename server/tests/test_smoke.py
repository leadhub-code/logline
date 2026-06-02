from subprocess import check_call, DEVNULL
import sys


def test_import():
    import logline_server


def test_run_server_help():
    check_call([sys.executable, '-m', 'logline_server', '--help'], stdout=DEVNULL)
