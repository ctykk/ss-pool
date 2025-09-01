from __future__ import annotations

import re
from abc import abstractmethod
from asyncio import Queue, Semaphore, create_task, gather
from base64 import b64decode
from collections import defaultdict
from heapq import heappop, heappush
from typing import TYPE_CHECKING, Callable, Collection, Generic, Protocol, TypeVar, runtime_checkable
from urllib.parse import unquote_plus

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from .core import Proxy


def group_by_location(
    proxies: Collection[Proxy], default: str = 'UNKNOWN'
) -> defaultdict[str, list[Proxy]]:
    """按地区为节点分组

    :param default: 匹配不到地区时的组
    :type default: str
    """
    if len(proxies) == 0:
        raise ValueError('len(proxies) == 0')

    result: defaultdict[str, list[Proxy]] = defaultdict(list)

    for p in proxies:
        if m := re.search(r'(.*?)\d+线 \| [A-Z]', p.name):
            id = str(m.group(1))
            result[id].append(p)
        else:
            result[default].append(p)

    return result


SS_URL: re.Pattern[str] = re.compile(r'^ss://([A-Za-z0-9]+)@([a-z0-9\.]+?\.com:\d{1,5})#(.*?)$')
"""解析 ss 链接的正则表达式"""


def from_base64(encoding: str, ignore: Collection[str | re.Pattern[str]] | None = None) -> list[Proxy]:
    """从 base64 编码中解析节点"""
    from .core import Proxy

    result: set[Proxy] = set()  # 去除重复项
    for line in b64decode(encoding).decode().splitlines():
        if m := SS_URL.search(line):
            encrypt_method, password = b64decode(m.group(1)).decode().split(':', 1)
            server_addr = m.group(2)
            name = unquote_plus(m.group(3))

            if ignore is not None:
                # 跳过忽略的节点
                if any(
                    pt in name if isinstance(pt, str) else pt.search(name) is not None for pt in ignore
                ):
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


async def test(proxy: Proxy, session: ClientSession | None = None, timeout: float = 10) -> bool:
    """测试节点有效性"""
    if timeout <= 0:
        raise ValueError(f'timeout={timeout} 必须为正整数')

    from aiohttp import ClientSession, ClientTimeout

    flag = False
    # session 为 None，就创建新 session，并在结束时关闭该新 session
    if session is None:
        flag = True
        session = ClientSession()

    try:
        async with session.get(
            url='http://ip-api.com/json',
            proxy=proxy.url,
            timeout=ClientTimeout(timeout),
            raise_for_status=True,
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
    *proxies: Proxy,
    session: ClientSession | None = None,
    timeout: float = 10,
    semaphore: Semaphore | None = None,
) -> dict[Proxy, bool]:
    """并发测试多个节点的有效性"""
    if timeout <= 0:
        raise ValueError(f'timeout={timeout} 必须为正整数')

    from aiohttp import ClientSession

    result: dict[Proxy, bool] = dict()
    # 限制 _run 的并发数
    semaphore = semaphore if semaphore is not None else Semaphore(10)

    async def _run(
        semaphore: Semaphore, proxy: Proxy, session: ClientSession, timeout: float
    ) -> tuple[Proxy, bool]:
        async with semaphore:
            return proxy, await test(proxy=proxy, session=session, timeout=timeout)

    flag = False
    # session 为 None，就创建新 session，并在结束时关闭该新 session
    if session is None:
        flag = True
        session = ClientSession()

    try:
        tasks = [create_task(_run(semaphore, p, session, timeout)) for p in proxies]
        for p, r in await gather(*tasks):
            result[p] = r
    finally:
        if flag:
            await session.close()

    return result


T = TypeVar('T')
P = TypeVar('P', contravariant=True)


@runtime_checkable
class SupportGtLt(Protocol, Generic[P]):
    __slots__ = tuple()

    @abstractmethod
    def __gt__(self, o: P, /) -> bool: ...
    def __lt__(self, o: P, /) -> bool: ...


DEFAULT_PRIORITY = lambda x: x
"""默认优先级计算方法"""


class CustomPriorityQueue(Generic[T, P], Queue[T]):
    """支持自定义优先级的优先队列"""

    def __init__(
        self, priority: Callable[[T], SupportGtLt[P]] = DEFAULT_PRIORITY, maxsize: int = 0
    ) -> None:
        self._prio = priority
        self._auto_id: int = 0
        super().__init__(maxsize)

    def _init(self, maxsize: int) -> None:
        self._queue: list[tuple[SupportGtLt[P], int, T]] = list()

    def _get(self) -> T:
        _, _, item = heappop(self._queue)
        return item

    def _put(self, item: T) -> None:
        self._auto_id += 1
        entity = (self._prio(item), self._auto_id, item)
        heappush(self._queue, entity)
