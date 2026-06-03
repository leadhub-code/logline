from argparse import Namespace

from logline_server.configuration import Configuration


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
