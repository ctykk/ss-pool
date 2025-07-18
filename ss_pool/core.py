from __future__ import annotations

import re
from asyncio import sleep
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from base64 import b64decode
from collections import defaultdict
from heapq import heappush
from random import randint
from typing import Any, Final, Iterable, Self
from urllib.parse import unquote_plus

from aiohttp import ClientSession


SSLOCAL = 'sslocal.exe'
TEST_URL = 'http://ip-api.com/json'


class ProxyError(Exception): ...


class Proxy:
    def __init__(
        self, server_addr: str, encrypt_method: str, password: str, name: str = 'UNKNOWN'
    ) -> None:
        self._server_addr: Final[str] = server_addr
        self._encrypt_method: Final[str] = encrypt_method
        self._password: Final[str] = password
        self.name: Final[str] = name
        """名称"""

        self.id: Final[tuple[str, int, str]]
        """地区 | 线路 | 编号"""
        if m := re.search(r'(.*?)(\d+)线 \| ([A-Z])', self.name):
            self.id = (m.group(1), int(m.group(2)), m.group(3))
        else:
            self.id = ('UNKNOWN', -1, 'UNKNOWN')

        self._process: Process | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        """代理的访问链接，仅能在代理进程已启动时使用"""
        if self.is_started():
            return f'http://localhost:{self._port}'
        raise ProxyError('代理未启动')

    def is_started(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, port: int | None = None) -> None:
        """启动代理进程，如果给定端口则仅尝试一次，否则随机尝试直到成功"""
        while True:
            try:
                return await self._start(randint(10000, 60000) if port is None else port)
            except ProxyError:
                if port is not None:
                    raise

    async def _start(self, port: int) -> None:
        if self.is_started():
            return

        process = await create_subprocess_exec(
            SSLOCAL,
            '-b',
            f'localhost:{port}',
            '-s',
            self._server_addr,
            '-m',
            self._encrypt_method,
            '-k',
            self._password,
            stderr=DEVNULL,
            stdout=DEVNULL,
        )
        # 等待一会后看看进程是否退出，如果退出代表启动失败
        await sleep(1)
        returncode = process.returncode
        if returncode is None:
            self._process = process
            self._port = port
            return

        raise ProxyError('启动失败，可能是端口已被占用')

    def stop(self) -> None:
        """关闭代理进程"""
        if self._process is not None:
            # 节点尚未关闭
            if self._process.returncode is None:
                self._process.terminate()
            self._process = None
        self._port = None

    @classmethod
    def from_base64(cls, encoding: str) -> list[Self]:
        """从 base64 中解析节点"""
        result: set[Self] = set()  # 去除重复项
        for line in b64decode(encoding).decode().splitlines():
            if m := re.search(r'^ss://([A-Za-z0-9]+)@([a-z0-9\.]+?\.com:\d{1,5})#(.*?)$', line):
                encrypt_method, password = b64decode(m.group(1)).decode().split(':')
                server_addr = m.group(2)
                name = unquote_plus(m.group(3))
                if name.startswith('套餐到期') or name.startswith('剩余流量'):
                    continue
                result.add(
                    cls(
                        server_addr=server_addr,
                        encrypt_method=encrypt_method,
                        password=password,
                        name=name,
                    )
                )
        return list(result)

    @classmethod
    def split_group(cls, proxies: Iterable[Self]) -> dict[str, list[Self]]:
        """将若干节点按 id[0] 分组，各组内按 id[1], id[2] 排序"""
        result: dict[str, list[Self]] = defaultdict(list)

        for p in proxies:
            heappush(result[p.id[0]], p)

        return result

    async def test(self, session: ClientSession | None = None) -> bool:
        """测试代理有效性"""
        flag = False
        # session 为 None，就创建新 session，并在方法结束时关闭该 session
        if session is None:
            flag = True
            session = ClientSession()

        try:
            async with session.get(TEST_URL, proxy=self.url, raise_for_status=True) as resp:
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

    def __repr__(self) -> str:
        return f'{type(self).__name__}(name="{self.name}", server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'

    def __str__(self) -> str:
        return f'name={self.name}, server_addr="{self._server_addr}"'

    def __hash__(self) -> int:
        """server_addr encrypt_method password 都相同的两个实例是同一个节点"""
        return hash(
            f'{type(self).__name__}(server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'
        )

    def __eq__(self, o: Any) -> bool:
        return isinstance(o, type(self)) and hash(self) == hash(o)

    def __gt__(self, o: Self) -> bool:
        return self.id > o.id

    def __lt__(self, o: Self) -> bool:
        return self.id < o.id
