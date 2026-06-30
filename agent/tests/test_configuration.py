from logging import DEBUG, INFO

import pytest

from logline_agent.configuration import Configuration, ConfigurationError
from logline_agent.main import get_argument_parser


BASE_CONF = '''\
server: 127.0.0.1:9999
client_token: topsecret
scan:
  - /var/log/*.log
'''


def load_conf(temp_dir, conf_yaml):
    cfg_path = temp_dir / 'configuration.yaml'
    cfg_path.write_text(conf_yaml)
    args = get_argument_parser().parse_args(['--conf', str(cfg_path)])
    return Configuration(args=args)


def test_log_file_level_defaults_to_info(temp_dir):
    conf = load_conf(temp_dir, BASE_CONF)
    assert conf.log_file_level == INFO


def test_log_file_level_can_be_set_to_debug(temp_dir):
    conf = load_conf(temp_dir, BASE_CONF + 'log:\n  level: debug\n')
    assert conf.log_file_level == DEBUG


def test_log_file_level_is_case_insensitive(temp_dir):
    conf = load_conf(temp_dir, BASE_CONF + 'log:\n  level: INFO\n')
    assert conf.log_file_level == INFO


def test_log_file_level_rejects_unknown_level(temp_dir):
    with pytest.raises(ConfigurationError):
        load_conf(temp_dir, BASE_CONF + 'log:\n  level: trace\n')
