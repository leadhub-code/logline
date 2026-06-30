from argparse import Namespace
from logging import DEBUG, INFO

import pytest

from logline_server.configuration import Configuration, ConfigurationError


def make_args(**overrides):
    args = dict(
        conf=None,
        log=None,
        bind=None,
        dest='/tmp',
        tls_cert=None,
        tls_key=None,
        tls_key_password_file=None,
        client_token_hash=['dummyhash'],
        reuse_port=False,
    )
    args.update(overrides)
    return Namespace(**args)


def test_reuse_port_defaults_to_false():
    conf = Configuration(args=make_args())
    assert conf.reuse_port is False


def test_reuse_port_enabled_via_cli():
    conf = Configuration(args=make_args(reuse_port=True))
    assert conf.reuse_port is True


def test_reuse_port_enabled_via_config_file(tmp_path):
    cfg_path = tmp_path / 'conf.yaml'
    cfg_path.write_text(
        'dest: dst\n'
        'reuse_port: true\n'
        'client_token_hashes: [dummyhash]\n')
    conf = Configuration(args=make_args(conf=str(cfg_path)))
    assert conf.reuse_port is True


def test_reuse_port_disabled_by_default_in_config_file(tmp_path):
    cfg_path = tmp_path / 'conf.yaml'
    cfg_path.write_text(
        'dest: dst\n'
        'client_token_hashes: [dummyhash]\n')
    conf = Configuration(args=make_args(conf=str(cfg_path)))
    assert conf.reuse_port is False


def test_log_file_level_defaults_to_info():
    conf = Configuration(args=make_args())
    assert conf.log_file_level == INFO


def write_conf_with_log_level(tmp_path, level):
    cfg_path = tmp_path / 'conf.yaml'
    cfg_path.write_text(
        'dest: dst\n'
        'client_token_hashes: [dummyhash]\n'
        f'log:\n  level: {level}\n')
    return cfg_path


def test_log_file_level_can_be_set_to_debug(tmp_path):
    cfg_path = write_conf_with_log_level(tmp_path, 'debug')
    conf = Configuration(args=make_args(conf=str(cfg_path)))
    assert conf.log_file_level == DEBUG


def test_log_file_level_is_case_insensitive(tmp_path):
    cfg_path = write_conf_with_log_level(tmp_path, 'INFO')
    conf = Configuration(args=make_args(conf=str(cfg_path)))
    assert conf.log_file_level == INFO


def test_log_file_level_rejects_unknown_level(tmp_path):
    cfg_path = write_conf_with_log_level(tmp_path, 'trace')
    with pytest.raises(ConfigurationError):
        Configuration(args=make_args(conf=str(cfg_path)))
