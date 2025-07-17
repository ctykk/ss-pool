from __future__ import annotations

from asyncio import Queue, Semaphore, Task, create_task, gather, get_running_loop, run, sleep
from asyncio.subprocess import DEVNULL, Process, create_subprocess_exec
from random import randint
from typing import Iterable, Self


SSLOCAL = 'sslocal.exe'


class ProxyError(Exception): ...


class Proxy:
    def __init__(self, server_addr: str, encrypt_method: str, password: str, name: str = '') -> None:
        self._server_addr: str = server_addr
        self._encrypt_method: str = encrypt_method
        self._password: str = password
        self.name: str = name

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
            if self._process.returncode is None:
                self._process.terminate()
            self._process = None
        self._port = None


class ProxyPool:
    # TODO
    def __init__(
        self, proxies: Iterable[Proxy], max_process: int = 5, recovery_delay: float = 60
    ) -> None:
        """
        :param max_process: 可同时使用的最大节点数
        :type max_process: int
        :param recovery_delay: 节点失效后的禁用时长（秒）
        :type recovery_delay: float
        """
        # 限制可同时使用的最大节点数
        self._max_process: int = max_process
        self._semaphore: Semaphore = Semaphore(self._max_process)
        self._recovery_delay: float = recovery_delay

        # NOTICE 优先使用已启动的节点，当节点被禁用时才启动新节点

        self._all_proxies: list[Proxy] = list(proxies)  # 总的节点列表
        self._free_proxies: Queue[Proxy] = Queue()  # 空闲的节点
        self._recovery_tasks: Queue[Task] = Queue()

    async def acquire(self) -> Proxy:
        """
        获取一个节点

        - 一个节点不可同时被多处使用
        - 阻塞，若已达到可同时使用的最大节点数
        """
        await self._semaphore.acquire()
        return await self._free_proxies.get()

    def release(self, proxy: Proxy) -> None:
        """释放一个节点"""
        self._free_proxies.put_nowait(proxy)
        self._semaphore.release()

    async def _recovery(self, proxy: Proxy) -> None:
        """关闭节点，等待一会后重新启动它"""
        proxy.stop()
        await sleep(self._recovery_delay)
        await self._start_and_enqueue(proxy)

    def mark_failure(self, proxy: Proxy) -> None:
        """标注一个节点为不可用"""
        _ = create_task(self._recovery(proxy))
        self._recovery_tasks.put_nowait(_)

    async def start(self) -> None:
        """启动 max_process 个代理进程"""
        await gather(*(self._start_and_enqueue(p) for p in self._all_proxies[: self._max_process]))

    async def _start_and_enqueue(self, proxy: Proxy) -> None:
        await proxy.start()
        await self._free_proxies.put(proxy)

    def stop(self) -> None:
        for p in self._all_proxies:
            p.stop()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, et, ev, eb) -> bool | None:
        self.stop()


if __name__ == '__main__':

    async def main():
        async with ProxyPool(
            [
                Proxy('', '', ''),
                Proxy('', '', ''),
                Proxy('', '', ''),
            ]
        ) as pool:
            pass

    run(main())
