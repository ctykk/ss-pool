from asyncio import gather, run
from collections import defaultdict

from aiohttp import ClientSession, DummyCookieJar
from loguru import logger

from ss_pool import ProxyPool, from_base64


async def main() -> None:
    with open('temp/proxy.base64', 'r') as fp:
        proxies = from_base64(fp.read(), ignore=('剩余', '套餐'))

    PROXY_HISTORY: dict[str, int] = defaultdict(int)
    """记录节点的使用次数"""

    async def fetch(session: ClientSession, pool: ProxyPool) -> None:
        """从代理池中获取代理看看能否访问"""
        # 成功就返回，失败就拿出下一节点重试
        while True:
            async with pool.use() as proxy:
                PROXY_HISTORY[proxy.name] += 1
                try:
                    # 非香港的节点都应失败
                    proxy_url = proxy.url if proxy.name.startswith('香港') else 'http://localhost:1'
                    async with session.get('https://cp.cloudflare.com', proxy=proxy_url) as resp:
                        resp.raise_for_status()
                        logger.success(proxy)
                        return

                except Exception as e:
                    logger.error(f'{proxy} {type(e).__name__}({e})')
                    proxy.disable()

    logger.info('加载代理池')
    async with ProxyPool(proxies) as pool:
        async with ClientSession(cookie_jar=DummyCookieJar()) as session:
            await gather(*[fetch(session, pool) for _ in range(300)])

    print('\n========== 节点使用记录 ==========')
    for p, c in sorted(PROXY_HISTORY.items(), key=lambda kv: kv[1], reverse=True):
        print(f'{p}\t{c}')

    async with ProxyPool(proxies) as pool:
        print(pool.count())


if __name__ == '__main__':
    run(main())
