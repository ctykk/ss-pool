# 检测 sslocal 命令是否存在
import shutil

if not shutil.which('sslocal'):
    raise FileNotFoundError('未找到 sslocal ( https://github.com/shadowsocks/shadowsocks-rust )')

from .core import Proxy, ProxyError, ProxyPool
from .util import from_base64, group_by_location, test, tests


__all__ = (
    'Proxy',
    'ProxyError',
    'ProxyPool',
    'from_base64',
    'group_by_location',
    'test',
    'tests',
)
