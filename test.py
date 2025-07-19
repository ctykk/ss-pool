from asyncio import gather, run
from collections import defaultdict

from aiohttp import ClientSession, DummyCookieJar
from loguru import logger

from ss_pool import ProxyPool, from_base64, group_by_location, tests


async def main() -> None:
    with open('./temp/7c151fe3.txt', 'r') as fp:
        proxies = from_base64(fp.read())

    PROXY_HISTORY: dict[str, int] = defaultdict(int)
    """记录节点的使用次数"""

    async def fetch(session: ClientSession, pool: ProxyPool) -> None:
        """从代理池中获取代理看看能否访问"""
        # 成功就返回，失败就拿出下一节点重试
        while True:
            async with pool.get() as proxy:
                PROXY_HISTORY[proxy.name] += 1
                try:
                    # 非香港的节点都应失败
                    proxy_url = proxy.url if proxy.name.startswith('香港') else 'http://localhost:12345'
                    async with session.get('https://cp.cloudflare.com', proxy=proxy_url) as resp:
                        resp.raise_for_status()
                        logger.success(proxy)
                        return

                except Exception as e:
                    logger.error(f'{proxy} {type(e).__name__}({e})')
                    pool.disable(proxy)

    async with ProxyPool(proxies, max_acquire=0, disable_until=5) as pool:
        async with ClientSession(cookie_jar=DummyCookieJar()) as session:
            await gather(*[fetch(session, pool) for _ in range(300)])

    print('\n========== 节点使用记录 ==========')
    for p, c in sorted(PROXY_HISTORY.items(), key=lambda kv: kv[1], reverse=True):
        print(f'{p}\t{c}')

    # # 启动所有节点
    # await gather(*[p.start() for p in proxies])

    # # 测试节点有效性
    # proxy_status = await tests(*proxies, timeout=10)
    # for p, s in proxy_status.items():
    #     print(f'{s}\t{p.name}')

    # # 关闭所有节点
    # for p in proxies:
    #     p.stop()

    # # 测试节点分组
    # proxy_group = group_by_location(*proxies)
    # for g, ps in proxy_group.items():
    #     print('\n========================================\n')
    #     print(g)
    #     for p in ps:
    #         print(p.name)


if __name__ == '__main__':
    run(main())
