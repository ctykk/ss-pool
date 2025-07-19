from __future__ import annotations

from asyncio import CancelledError, Queue, QueueEmpty, QueueFull, Semaphore, create_task, gather, sleep
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from contextlib import asynccontextmanager
from random import randint
from time import monotonic
from typing import Any, AsyncGenerator, Collection, Final, Self

from loguru import logger

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

    async def start(self, port: int | None = None) -> None:
        """
        启动节点，如果给定端口则仅尝试一次，否则随机尝试直到成功

        :raises ProxyError: 传入了端口且端口不可用
        """
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
        """关闭节点"""
        if self._process is not None:
            # 节点尚未关闭
            if self._process.returncode is None:
                self._process.terminate()
            self._process = None
        self._port = None

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

    """
    # NOTICE
    - 节点中途失效时如何暂时禁用它？（因不同调用方的实现判定不同，是否需要禁用由调用方判定）
    - 节点过了禁用时间后如何恢复它？
    - 如何限制同时使用的最大节点数？（只启动这么多个节点，其余的节点作为当前使用节点被禁用后的备胎）
    """

    def __init__(
        self, proxies: Collection[Proxy], max_acquire: int = 0, disable_until: float = 60
    ) -> None:
        """
        :param max_acquire: 同时能使用的最大节点数（小于等于 0 为无限制）
        :param disable_until: 节点禁用持续时间（秒）
        """
        self._enable_sem: bool = max_acquire > 0  # 是否启用 acquire_sem
        if self._enable_sem:
            self._acquire_sem = Semaphore(max_acquire)  # 限制同时能使用的最大节点数
        self._disable_until: float = disable_until

        self._all: list[Proxy] = list(set(proxies))  # 所有节点（去重）
        self._active: Queue[Proxy] = Queue(maxsize=max_acquire)  # 激活的节点
        self._standby: Queue[Proxy] = Queue()  # 后备节点
        self._disabled: dict[Proxy, float] = dict()  # 被禁用节点与其禁用到期时间

    async def acquire(self) -> Proxy:
        """获取一个节点"""
        # 持续从 active 中获取节点，直到找到为未被禁用的节点
        if self._enable_sem:
            await self._acquire_sem.acquire()

        # 从 active 获取节点，若 active 已空从 standby 获取
        try:
            logger.debug('尝试获取 active 节点')
            proxy = self._active.get_nowait()
            logger.debug(f'成功获取 active 节点 {proxy}')
        except QueueEmpty:
            logger.debug('无 active 节点，尝试获取 standby 节点')
            proxy = await self._standby.get()
            logger.debug(f'成功获取 standby 节点 {proxy}')

        # 如果节点被禁用就等待禁用结束（standby 的队头应当为最久未被使用的节点）
        if self._is_disabled(proxy):
            delay = round(self._disabled[proxy] - monotonic(), 2)
            logger.debug(f'节点已被禁用，休眠 {delay} 秒 {proxy}')
            # await sleep(self._disabled[proxy] - monotonic())
            await sleep(delay)
            logger.debug(f'休眠结束 {proxy}')
            if proxy in self._disabled:
                self._disabled.pop(proxy)

        # 节点未启动就启动它
        if not proxy.is_started():
            logger.debug(f'启动节点 {proxy}')
            await proxy.start()

        return proxy

    def release(self, proxy: Proxy) -> None:
        """释放一个节点"""
        # 节点仍处于禁用状态，就关闭其进程，并放回 standby
        if self._is_disabled(proxy):
            logger.debug(f'关闭已禁用节点 {proxy}')
            proxy.stop()
            logger.debug(f'尝试将已禁用节点放入 standby {proxy}')
            self._standby.put_nowait(proxy)
            logger.debug(f'成功将已禁用节点放入 standby {proxy}')
            return

        # 节点仍可用，将节点放回 active，active 已满就放到 standby
        try:
            logger.debug(f'尝试放回 active 节点 {proxy}')
            self._active.put_nowait(proxy)
            logger.debug(f'成功放回 active 节点 {proxy}')
        except QueueFull:
            logger.debug(f'active 已满，尝试放回 standby 节点 {proxy}')
            self._standby.put_nowait(proxy)
            logger.debug(f'成功放回 standby 节点 {proxy}')

        if self._enable_sem:
            self._acquire_sem.release()

    @asynccontextmanager
    async def get(self) -> AsyncGenerator[Proxy]:
        """acquire + release"""
        proxy = await self.acquire()
        try:
            yield proxy
        finally:
            self.release(proxy)

    def disable(self, proxy: Proxy) -> None:
        """禁用该节点"""
        self._disabled[proxy] = monotonic() + self._disable_until

    def _is_disabled(self, proxy: Proxy) -> bool:
        """节点是否处于禁用状态"""
        return proxy in self._disabled and self._disabled[proxy] > monotonic()

    async def _recover(self) -> None:
        """恢复任务"""
        while True:
            # 删除 disable 中所有可恢复的节点
            now = monotonic()
            recoverable = (p for p, t in self._disabled.items() if t < now)
            for p in recoverable:
                logger.debug(f'恢复节点 {p}')
                self._disabled.pop(p)

            try:
                await sleep(self._disable_until / 2)
            except CancelledError:
                return

    async def start(self) -> Self:
        """启动代理池"""
        # 启动所有节点
        await gather(*[p.start() for p in self._all])
        # 测试所有节点的有效性
        proxy_status = await tests(*self._all, timeout=10)
        for p, s in proxy_status.items():
            # 节点通过检测，将节点放入 active，active 已满就放到 standby
            if s:
                try:
                    self._active.put_nowait(p)
                except QueueFull:
                    self._standby.put_nowait(p)
            # 节点未通过检测，关闭该节点的进程
            else:
                p.stop()

        if self._active.qsize() == 0:
            raise ProxyError('无可用节点')

        # 启动恢复被禁用节点任务
        self._recovery_task = create_task(self._recover())
        return self

    def stop(self) -> None:
        """关闭代理池"""
        self._recovery_task.cancel()
        for p in self._all:
            p.stop()

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, et, ev, eb) -> bool | None:
        self.stop()
