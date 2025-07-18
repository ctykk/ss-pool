from __future__ import annotations

import re
from base64 import b64decode
from collections import defaultdict
from urllib.parse import unquote_plus

from aiohttp import ClientSession

from .core import Proxy


def group_by_location(*proxies: Proxy) -> dict[str, list[Proxy]]:
    """按地区为节点分组"""
    if len(proxies) == 0:
        raise ValueError('len(proxies) == 0')

    result: dict[str, list[Proxy]] = defaultdict(list)

    for p in proxies:
        if m := re.search(r'(.*?)\d+线 \| [A-Z]', p.name):
            id = str(m.group(1))
            result[id].append(p)
        else:
            result['UNKNOWN'].append(p)

    return result


def from_base64(encoding: str) -> list[Proxy]:
    """从 base64 编码中解析节点"""
    result: set[Proxy] = set()  # 去除重复项
    for line in b64decode(encoding).decode().splitlines():
        if m := re.search(r'^ss://([A-Za-z0-9]+)@([a-z0-9\.]+?\.com:\d{1,5})#(.*?)$', line):
            encrypt_method, password = b64decode(m.group(1)).decode().split(':')
            server_addr = m.group(2)
            name = unquote_plus(m.group(3))
            if name.startswith('套餐到期') or name.startswith('剩余流量'):
                continue
            result.add(
                Proxy(
                    server_addr=server_addr,
                    encrypt_method=encrypt_method,
                    password=password,
                    name=name,
                )
            )
    return list(result)


TEST_URL = 'http://ip-api.com/json'
"""测试代理有效性的接口"""


async def test(proxy: Proxy, session: ClientSession | None = None) -> bool:
    """测试代理有效性"""
    flag = False
    # session 为 None，就创建新 session，并在结束时关闭该新 session
    if session is None:
        flag = True
        session = ClientSession()

    try:
        async with session.get(TEST_URL, proxy=proxy.url, raise_for_status=True) as resp:
            # 若返回的 countryCode == CN 或在请求时出错了就返回 False
            resp_json = await resp.json()
            if resp_json['countryCode'] == 'CN':
                return False
            return True
    except Exception:
        return False
    finally:
        if flag:
            await session.close()
