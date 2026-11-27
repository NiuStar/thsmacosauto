"""Standalone command-line entry point."""
import argparse
import json
import signal
import sys

from . import api
from ._broker.private_policy_read import read_private
from ._broker.server import strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description='同花顺 macOS AX 本机 HTTP 服务')
    parser.add_argument('--config', required=True, help='绝对路径：私有 JSON 配置文件')
    parser.add_argument('--check-config', action='store_true', help='只检查配置、令牌和账户绑定，不监听或访问 AX')
    args = parser.parse_args(argv)
    service = None
    try:
        config = strict_json(read_private(args.config, limit=32768))
        if args.check_config:
            api.preflight(config)
            print('{"status":"CONFIG_VALID","execution_enabled":false}', flush=True)
            return 0
        if sys.platform != 'darwin':
            raise ValueError('MACOS_REQUIRED')
        service = api.create(config)
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        host, port, *_ = service.server.server_address
        print(json.dumps({'status':'LISTENING','host':host,'port':port,'execution_enabled':False}), flush=True)
        service.server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print('THS service failed: '+type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
