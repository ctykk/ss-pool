from __future__ import annotations

import re
from asyncio import create_task, gather
from base64 import b64decode
from collections import defaultdict
from typing import Collection
from urllib.parse import unquote_plus

from aiohttp import ClientSession, ClientTimeout

from .core import Proxy


def group_by_location(*proxies: Proxy, default: str = 'UNKNOWN') -> dict[str, list[Proxy]]:
    """按地区为节点分组

    :param default: 匹配不到地区时的组
    :type default: str
    """
    if len(proxies) == 0:
        raise ValueError('len(proxies) == 0')

    result: dict[str, list[Proxy]] = defaultdict(list)

    for p in proxies:
        if m := re.search(r'(.*?)\d+线 \| [A-Z]', p.name):
            id = str(m.group(1))
            result[id].append(p)
        else:
            result[default].append(p)

    return result


SS_URL: re.Pattern[str] = re.compile(r'^ss://([A-Za-z0-9]+)@([a-z0-9\.]+?\.com:\d{1,5})#(.*?)$')
"""解析 ss 链接的正则表达式"""
DEFAULT_IGNORE: tuple[re.Pattern[str], ...] = (
    re.compile('套餐到期'),
    re.compile('剩余流量'),
    re.compile('距离下次重置剩余'),
)
"""默认忽略的节点"""


def from_base64(encoding: str, ignore: Collection[re.Pattern[str]] = DEFAULT_IGNORE) -> list[Proxy]:
    """从 base64 编码中解析节点

    ---

    默认忽略
    - 套餐到期
    - 剩余流量
    - 距离下次重置剩余
    """
    result: set[Proxy] = set()  # 去除重复项
    for line in b64decode(encoding).decode().splitlines():
        if m := SS_URL.search(line):
            encrypt_method, password = b64decode(m.group(1)).decode().split(':', 1)
            server_addr = m.group(2)
            name = unquote_plus(m.group(3))

            # 跳过忽略的节点
            if any(pt.search(name) is not None for pt in ignore):
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


TEST_URL: str = 'http://ip-api.com/json'
"""测试节点有效性的接口"""


async def test(proxy: Proxy, session: ClientSession | None = None, timeout: float = 30) -> bool:
    """测试节点有效性"""
    flag = False
    # session 为 None，就创建新 session，并在结束时关闭该新 session
    if session is None:
        flag = True
        session = ClientSession()

    try:
        async with session.get(
            TEST_URL, proxy=proxy.url, timeout=ClientTimeout(timeout), raise_for_status=True
        ) as resp:
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


async def tests(
    *proxies: Proxy, session: ClientSession | None = None, timeout: float = 30
) -> dict[Proxy, bool]:
    """并发测试多个节点的有效性"""
    result: dict[Proxy, bool] = dict()

    async def _run(proxy: Proxy, session: ClientSession, timeout: float) -> tuple[Proxy, bool]:
        return proxy, await test(proxy, session, timeout)

    flag = False
    # session 为 None，就创建新 session，并在结束时关闭该新 session
    if session is None:
        flag = True
        session = ClientSession()

    try:
        tasks = [create_task(_run(p, session, timeout)) for p in proxies]
        for p, r in await gather(*tasks):
            result[p] = r
    finally:
        if flag:
            await session.close()

    return result
