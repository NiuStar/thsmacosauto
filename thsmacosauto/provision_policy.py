"""Create a local account binding without accepting identifiers in argv."""
import argparse
from getpass import getpass, GetPassWarning
import json
import os
import re
import stat
import sys
import warnings

from ._broker.private_policy_read import parent_chain
from ._broker.single_fixed_account import BROKER, IDENTITY_KIND, digest


def create_policy(output, shenzhen, shanghai=''):
    scopes = {'深圳': shenzhen}
    if shanghai:
        scopes['上海'] = shanghai
    if any(type(value) is not str or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value)
           for value in scopes.values()):
        raise ValueError('INVALID_SHAREHOLDER_SCOPE')
    hashes = {market: digest('ths:shareholder', value) for market, value in sorted(scopes.items())}
    material = json.dumps([BROKER, hashes], ensure_ascii=False, separators=(',', ':'))
    document = {'broker': BROKER, 'account_identity_kind': IDENTITY_KIND,
                'account_key': digest('ths:single-fixed-account:r4:v1', material),
                'scope_hashes': hashes, 'raw_shenzhen_transport_scope': shenzhen}
    if shanghai:
        document['raw_shanghai_transport_scope'] = shanghai
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode()
    with parent_chain(output) as (directory, name, check):
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('PRIVATE_PARENT')
        check()
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.fsync(directory)


def main():
    parser = argparse.ArgumentParser(description='在私有目录中创建本机单账户绑定')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print('需要交互式终端，不通过命令行参数传递股东标识。', file=sys.stderr)
        return 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            create_policy(args.output, getpass('深圳股东标识: '), getpass('上海股东标识（可留空）: '))
    except (OSError, ValueError, EOFError, GetPassWarning):
        print('未创建有效绑定：请核对输入、私有目录权限，且目标文件不能已存在。', file=sys.stderr)
        return 1
    print('已创建本机账户绑定。尚未验证券商会话。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
