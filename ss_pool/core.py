from __future__ import annotations

from asyncio import Queue, QueueEmpty, QueueFull, Semaphore, gather, get_event_loop, sleep
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
        """
        :param server_addr: 代理服务器地址
        :param encrypt_method: 加密方法
        :param password: 密码
        :param name: 标识名（可选）
        """
        self._server_addr: Final[str] = server_addr
        self._encrypt_method: Final[str] = encrypt_method
        self._password: Final[str] = password
        self.name: Final[str] = name

        # 节点状态
        self.disable_until: float = monotonic()  # 禁用解除时间
        self._process: Process | None = None  #  节点进程
        self._port: int | None = None  #  本地绑定端口

        # 注册终结器确保资源清理（防止内存泄漏）
        self._finalizer = finalize(self, Proxy.stop, self)

    @property
    def url(self) -> str:
        """
        获取节点访问 URL（仅当节点已启动时有效）

        :return: 本地代理 URL（http://localhost:{port}）

        :raises ProxyError: 节点未启动
        """
        if self.is_started():
            return f'http://localhost:{self._port}'
        raise ProxyError('代理未启动')

    def is_started(self) -> bool:
        """检查节点进程是否正在运行"""
        return self._process is not None and self._process.returncode is None

    async def start(self, port: int | None = None, acl: str | Path | None = None) -> None:
        """
        启动节点

        - 指定端口：仅尝试一次，失败则报错
        - 未指定端口：随机尝试端口直至成功

        :raises ProxyError: 已指定端口且端口被占用
        :raises FileNotFoundError: 已指定 ACL 文件但文件不存在
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
        """内部启动实现"""
        if self.is_started():
            return

        # 构建命令行参数
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
        # 添加 ACL 支持
        if acl is not None:
            if not acl.is_file():
                raise FileNotFoundError(acl)
            cmd = cmd + ('--acl', str(acl))

        # 启动进程
        process = await create_subprocess_exec(
            *cmd,
            stderr=DEVNULL,
            stdout=DEVNULL,
        )

        # 等待进程初始化
        await sleep(1)

        # 验证启动状态
        returncode = process.returncode
        if returncode is None:
            self._process = process
            self._port = port
            return

        raise ProxyError(f'启动失败（returncode={process.returncode}），可能是端口 {port} 已被占用')

    def stop(self) -> None:
        """停止进程并清理资源"""
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
            self._process = None
        self._port = None

    def is_disabled(self) -> bool:
        """检查节点当前是否被禁用"""
        return self.disable_until > monotonic()

    def __repr__(self) -> str:
        return f'{type(self).__name__}(name="{self.name}", server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'

    def __str__(self) -> str:
        return f'name="{self.name}", server_addr="{self._server_addr}"'

    def __hash__(self) -> int:
        """服务器地址相同即视为相等"""
        return hash(f'{type(self).__name__}(server_addr="{self._server_addr}")')

    def __eq__(self, o: Any) -> bool:
        """服务器地址相同即视为相等"""
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
        :param proxies: 初始代理集合
        :param max_acquire: 最大并发获取数（小于等于 0 表示无限制）
        :param test_timeout: 代理测试超时时间（秒）（小于等于 0 表示跳过测试）
        :param acl: 代理路由规则配置文件路径（可选）
        """
        self._acl = acl
        self._max_acquire = max_acquire
        self._test_timeout = test_timeout

        # 所有节点（已去重）
        self._all: list[Proxy] = list(set(proxies))

        # 按禁用解除时间排序
        ## 活跃队列：存放当前可用代理，容量限制为 max_acquire
        self._active: Queue[Proxy] = CustomPriorityQueue(lambda p: p.disable_until, maxsize=max_acquire)
        ## 后备队列：存放被禁用或活跃队列满时的代理
        self._standby: Queue[Proxy] = CustomPriorityQueue(lambda p: p.disable_until)

    async def acquire(self) -> Proxy:
        """获取可用节点"""

        # 等待信号量许可
        await self._acquire_sem.acquire()

        try:
            # 优先从活跃队列获取，空时转后备队列
            try:
                proxy = self._active.get_nowait()
            except QueueEmpty:
                proxy = await self._standby.get()

            # 若代理被禁用，等待至禁用解除
            if proxy.is_disabled():
                await sleep(proxy.disable_until - monotonic())

            # 确保代理进程已启动
            if not proxy.is_started():
                await proxy.start(acl=self._acl)

            return proxy

        # 异常时释放信号量避免死锁
        except Exception:
            self._acquire_sem.release()
            raise

    def release(self, proxy: Proxy) -> None:
        """
        释放节点

        :param proxy: 要释放的节点
        """
        try:
            # 节点已禁用：停止进程并放入备用队列
            if proxy.is_disabled():
                proxy.stop()
                self._standby.put_nowait(proxy)
            else:
                # 节点仍可用：尝试放回活跃队列，满时转入后备队列
                try:
                    self._active.put_nowait(proxy)
                except QueueFull:
                    proxy.stop()
                    self._standby.put_nowait(proxy)

        finally:
            self._acquire_sem.release()

    @asynccontextmanager
    async def use(self) -> AsyncGenerator[Proxy]:
        """获取和释放节点"""
        proxy = await self.acquire()
        try:
            yield proxy
        finally:
            self.release(proxy)

    def disable(self, proxy: Proxy, second: float = 60) -> None:
        """
        禁用代理指定时间

        :param proxy: 要禁用的节点
        :param second: 禁用时长（秒）
        """
        proxy.disable_until = monotonic() + second

    async def start(self, timeout: float) -> None:
        """
        初始化代理池并验证节点可用性

        :raises ProxyError: 无可用代理
        """
        # timeout <= 0 时跳过测试直接初始化
        if timeout <= 0:
            for p in self._all:
                try:
                    self._active.put_nowait(p)
                except QueueFull:
                    self._standby.put_nowait(p)

        else:
            # 并行启动所有节点
            await gather(*[p.start(acl=self._acl) for p in self._all])
            # 批量测试节点有效性
            proxy_status = await tests(*self._all, timeout=timeout)
            for p, s in proxy_status.items():
                # 有效节点优先放入活跃队列，满时放入后备队列
                if s:
                    try:
                        self._active.put_nowait(p)
                    except QueueFull:
                        p.stop()
                        self._standby.put_nowait(p)
                # 失败节点直接关闭
                else:
                    p.stop()

        # 确保至少有一个可用节点
        if self._active.qsize() == 0:
            raise ProxyError('无可用节点')

        # 设置信号量控制并发访问（不超过可用节点数）
        print(self._max_acquire, self.count())
        if self._max_acquire <= 0:
            self._acquire_sem = Semaphore(self.count())
        else:
            self._acquire_sem = Semaphore(min(self._max_acquire, self.count()))

    def stop(self) -> None:
        """关闭代理池：停止所有节点的进程"""
        for p in self._all:
            p.stop()

    async def __aenter__(self) -> Self:
        await self.start(self._test_timeout)
        return self

    async def __aexit__(self, et, ev, eb) -> bool | None:
        self.stop()

    def count(self) -> int:
        """
        获取节点总数（未被使用的可用节点数）

        NOTICE: 仅包含活跃队列和后备队列中的节点数，不代表总节点数
        """
        return self._active.qsize() + self._standby.qsize()
