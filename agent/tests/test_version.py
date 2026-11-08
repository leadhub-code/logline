from pathlib import Path
import re
from subprocess import check_output
import sys

from logline_agent import version


project_dir = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    pyproject = (project_dir / 'pyproject.toml').read_text()
    m = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert m, 'version not found in pyproject.toml'
    assert version.__version__ == m.group(1)


def test_git_commit_line_can_be_stamped():
    # Build scripts (server/Dockerfile) replace this exact line with the commit hash.
    source = Path(version.__file__).read_text()
    assert re.search(r'^git_commit = None$', source, re.MULTILINE)


def test_version_string_uses_stamped_commit(monkeypatch):
    monkeypatch.setattr(version, 'git_commit', 'abc1234')
    assert version.get_version_string() == f'logline-agent {version.__version__} (commit abc1234)'


def test_version_string_without_commit(monkeypatch):
    monkeypatch.setattr(version, 'git_commit', None)
    monkeypatch.setattr(version, 'get_git_commit_from_checkout', lambda: None)
    assert version.get_version_string() == f'logline-agent {version.__version__}'


def test_version_option():
    out = check_output([sys.executable, '-m', 'logline_agent', '--version'], text=True)
    assert out.startswith(f'logline-agent {version.__version__}')


def test_git_commit_from_checkout():
    commit = version.get_git_commit_from_checkout()
    if commit is None:
        # Not running from a git checkout (or the file is not committed yet).
        return
    assert re.fullmatch(r'[0-9a-f]{40}(-dirty)?', commit)
