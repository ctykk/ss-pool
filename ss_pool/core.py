from __future__ import annotations

from asyncio import Queue, gather, sleep
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from contextlib import asynccontextmanager
from random import randint
from typing import Any, AsyncGenerator, Collection, Final, Self

from .util import tests


SSLOCAL = 'sslocal.exe'


class ProxyError(Exception): ...


class Proxy:
    def __init__(
        self, server_addr: str, encrypt_method: str, password: str, name: str = 'UNKNOWN'
    ) -> None:
        self._server_addr: Final[str] = server_addr
        self._encrypt_method: Final[str] = encrypt_method
        self._password: Final[str] = password
        self.name: Final[str] = name

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


class ProxyPool:
    """网络代理池"""

    """
    TODO
    - 代理中途失效时如何暂时禁用它？
    - 代理过了禁用时间后如何恢复它？
    - 如何限制同时启动的最大节点数？（只启动这么多个节点，其余的节点作为当前已启动节点被禁用后的备胎）
    """

    def __init__(self, proxies: Collection[Proxy]) -> None:
        self._all_proxies: list[Proxy] = list(set(proxies))  # 所有节点（去重）
        self._available_proxies: Queue[Proxy] = Queue()  # 可用的节点

    async def acquire(self) -> Proxy:
        """获取一个节点"""
        return await self._available_proxies.get()

    def release(self, proxy: Proxy) -> None:
        """释放一个节点"""
        self._available_proxies.put_nowait(proxy)

    @asynccontextmanager
    async def get(self) -> AsyncGenerator[Proxy]:
        proxy = await self.acquire()
        try:
            yield proxy
        finally:
            self.release(proxy)

    async def start(self) -> Self:
        """启动代理池"""
        # 启动所有节点
        await gather(*[p.start() for p in self._all_proxies])
        # 测试所有节点的有效性
        proxy_status = await tests(*self._all_proxies, timeout=10)
        # 去除所有无效节点
        for p, s in proxy_status.items():
            # 节点通过检测
            if s:
                self._available_proxies.put_nowait(p)
            else:
                p.stop()
        return self

    def stop(self) -> None:
        """关闭代理池"""
        for p in self._all_proxies:
            p.stop()

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, et, ev, eb) -> bool | None:
        self.stop()

    async def _test_proxies_available(self) -> None:
        """每个一段时间从代理池中拿出部分节点检验代理有效性"""
