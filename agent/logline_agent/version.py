from argparse import SUPPRESS, Action
from pathlib import Path
from subprocess import DEVNULL, CalledProcessError, TimeoutExpired, check_output


# Keep in sync with `version` in pyproject.toml (checked by tests/test_version.py).
__version__ = '0.0.1'

# Git commit the package was built from. Stays None in the repository; builds
# replace this exact line with the commit hash (see server/Dockerfile). When
# None, the commit is looked up from the git checkout the package is running
# from, if there is one.
git_commit = None


def get_version_string(prog='logline-agent'):
    commit = git_commit or get_git_commit_from_checkout()
    if commit:
        return f'{prog} {__version__} (commit {commit})'
    return f'{prog} {__version__}'


def get_git_commit_from_checkout():
    '''
    Return the HEAD commit (with a "-dirty" suffix if there are uncommitted
    changes) when this module is a tracked file in a git checkout, i.e. when
    running from source. Return None otherwise, e.g. when installed from a
    wheel into a virtualenv that happens to live inside some git repository.
    '''
    here = Path(__file__).resolve()
    try:
        def git(*args):
            return check_output(['git', '-C', str(here.parent), *args], stderr=DEVNULL, timeout=5, text=True).strip()
        git('ls-files', '--error-unmatch', here.name)
        commit = git('rev-parse', 'HEAD')
        if git('status', '--porcelain', '--untracked-files=no'):
            commit += '-dirty'
        return commit
    except (OSError, CalledProcessError, TimeoutExpired):
        # git is not installed, this is not a checkout, or the file is not tracked
        return None


class VersionAction(Action):
    '''
    Like argparse's built-in "version" action, but builds the version string
    only when the option is used, so the git lookup does not run on every start.
    '''

    def __init__(self, option_strings, dest=SUPPRESS, default=SUPPRESS, help="show program's version number and exit"):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Print to stdout like argparse's built-in action does.
        print(get_version_string())
        parser.exit()
