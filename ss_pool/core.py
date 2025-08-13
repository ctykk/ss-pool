from __future__ import annotations

from asyncio import Queue, QueueEmpty, QueueFull, Semaphore, gather, sleep
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from contextlib import asynccontextmanager
from pathlib import Path
from random import randint
from time import monotonic
from typing import Any, AsyncGenerator, Collection, Final, Self
from weakref import finalize

from .util import CustomPriorityQueue, tests


class ProxyError(Exception): ...


class Proxy:
    def __init__(
        self, server_addr: str, encrypt_method: str, password: str, name: str = 'UNKNOWN'
    ) -> None:
        self._server_addr: Final[str] = server_addr
        self._encrypt_method: Final[str] = encrypt_method
        self._password: Final[str] = password
        self.name: Final[str] = name

        self.disable_until: float = monotonic()

        self._process: Process | None = None
        self._port: int | None = None

        # NOTICE 可能会出现先是手动调用 stop，然后又因为实例被销毁而二次调用 stop 的情况
        self._finalizer = finalize(self, Proxy.stop, self)  # 实例被销毁时调用 stop

    @property
    def url(self) -> str:
        """
        代理的访问链接，仅能在节点已启动时使用

        :raises ProxyError: 节点未启动
        """
        if self.is_started():
            return f'http://localhost:{self._port}'
        raise ProxyError('代理未启动')

    def is_started(self) -> bool:
        """节点是否启动"""
        return self._process is not None and self._process.returncode is None

    async def start(self, port: int | None = None, acl: str | Path | None = None) -> None:
        """
        启动节点，如果给定端口则仅尝试一次，否则随机尝试直到成功

        :raises ProxyError: 传入了端口且端口不可用
        :raises FileNotFoundError: 传入了 acl 但对应文件不存在
        :raises IsADirectoryError: 传入了 acl 但对应路径为文件
        """
        while True:
            try:
                return await self._start(
                    randint(10000, 60000) if port is None else port,
                    acl=Path(acl) if isinstance(acl, str) else acl,
                )
            except ProxyError:
                if port is not None:
                    raise

    async def _start(self, port: int, acl: Path | None) -> None:
        if self.is_started():
            return

        cmd = (
            'sslocal',
            '-b',
            f'localhost:{port}',
            '-s',
            self._server_addr,
            '-m',
            self._encrypt_method,
            '-k',
            self._password,
        )
        # 添加 acl 支持
        if acl is not None:
            if not acl.exists():
                raise FileNotFoundError(acl)
            if not acl.is_file():
                raise IsADirectoryError(acl)
            cmd = cmd + ('--acl', str(acl))

        process = await create_subprocess_exec(
            *cmd,
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
        """关闭节点"""
        if self._process is not None:
            # 节点尚未关闭
            if self._process.returncode is None:
                self._process.terminate()
            self._process = None
        self._port = None

    def is_disabled(self) -> bool:
        return self.disable_until > monotonic()

    def __repr__(self) -> str:
        return f'{type(self).__name__}(name="{self.name}", server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'

    def __str__(self) -> str:
        return f'name="{self.name}", server_addr="{self._server_addr}"'

    def __hash__(self) -> int:
        """server_addr encrypt_method password 都相同的两个实例是同一个节点"""
        return hash(
            f'{type(self).__name__}(server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'
        )

    def __eq__(self, o: Any) -> bool:
        return isinstance(o, type(self)) and hash(self) == hash(o)


class ProxyPool:
    """网络代理池"""

    def __init__(
        self,
        proxies: Collection[Proxy],
        max_acquire: int = 0,
        test_timeout: float = 10,
        acl: str | Path | None = None,
    ) -> None:
        """
        :param max_acquire: 同时能使用的最大节点数（小于等于 0 为无限制）
        :param disable_until: 节点禁用持续时间（秒）
        :param test_timeout: 测试节点可用性的超时时间
        :param acl: 路由配置
        """
        self._acl = acl

        self._enable_sem: bool = max_acquire > 0  # 是否启用 acquire_sem
        if self._enable_sem:
            self._acquire_sem = Semaphore(max_acquire)  # 限制同时能使用的最大节点数
        self._test_timeout: float = test_timeout

        self._all: list[Proxy] = list(set(proxies))  # 所有节点（去重）
        # 以 proxy._disable_until 作为队列优先级
        self._active: Queue[Proxy] = CustomPriorityQueue(
            lambda p: p.disable_until, maxsize=max_acquire
        )  # 激活的节点
        self._standby: Queue[Proxy] = CustomPriorityQueue(lambda p: p.disable_until)  # 后备节点

    async def acquire(self) -> Proxy:
        """获取一个节点"""
        # 持续从 active 中获取节点，直到找到为未被禁用的节点
        if self._enable_sem:
            await self._acquire_sem.acquire()

        # 从 active 获取节点，若 active 已空从 standby 获取
        try:
            proxy = self._active.get_nowait()
        except QueueEmpty:
            proxy = await self._standby.get()

        # 如果节点被禁用就等待禁用结束（standby 的队头应当为最久未被使用的节点）
        if proxy.is_disabled():
            await sleep(proxy.disable_until - monotonic())

        # 节点未启动就启动它
        if not proxy.is_started():
            await proxy.start(acl=self._acl)

        return proxy

    def release(self, proxy: Proxy) -> None:
        """释放一个节点"""
        # 节点仍处于禁用状态，就关闭其进程，并放回 standby
        if proxy.is_disabled():
            proxy.stop()
            self._standby.put_nowait(proxy)
        else:
            # 节点仍可用，将节点放回 active，active 已满就放到 standby
            try:
                self._active.put_nowait(proxy)
            except QueueFull:
                proxy.stop()
                self._standby.put_nowait(proxy)

        if self._enable_sem:
            self._acquire_sem.release()

    @asynccontextmanager
    async def use(self) -> AsyncGenerator[Proxy]:
        """acquire + release"""
        proxy = await self.acquire()
        try:
            yield proxy
        finally:
            self.release(proxy)

    def disable(self, proxy: Proxy, second: float = 60) -> None:
        """禁用该节点"""
        proxy.disable_until = monotonic() + second

    async def start(self) -> Self:
        """启动代理池"""
        # 启动所有节点
        await gather(*[p.start(acl=self._acl) for p in self._all])
        # 测试所有节点的有效性
        proxy_status = await tests(*self._all, timeout=self._test_timeout)
        for p, s in proxy_status.items():
            # 节点通过检测，将节点放入 active，active 已满就放到 standby
            if s:
                try:
                    self._active.put_nowait(p)
                except QueueFull:
                    p.stop()
                    self._standby.put_nowait(p)
            # 节点未通过检测，关闭该节点的进程
            else:
                p.stop()

        if self._active.qsize() == 0:
            raise ProxyError('无可用节点')

        return self

    def stop(self) -> None:
        """关闭代理池"""
        # 关闭所有节点
        for p in self._all:
            p.stop()

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, et, ev, eb) -> bool | None:
        self.stop()

    def count(self) -> int:
        """节点总数 (_active + _standby)"""
        return self._active.qsize() + self._standby.qsize()
