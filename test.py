from asyncio import gather, run

from aiohttp import ClientSession, DummyCookieJar
from colorama import init, Fore

from ss_pool import ProxyPool, from_base64, group_by_location, tests


async def main():
    with open('./temp/7c151fe3.txt', 'r') as fp:
        proxies = from_base64(fp.read())

    async def get_google(session: ClientSession, pool: ProxyPool) -> None:
        """从代理池中获取代理看看能否访问谷歌"""
        async with pool.get() as proxy:
            try:
                async with session.get('https://www.google.com', proxy=proxy.url) as resp:
                    resp.raise_for_status()
                    html = await resp.text()
                    print('\n====================')
                    print(proxy)
                    print(Fore.GREEN + html[:150])
                    print('++++++++++++++++++++\n')
            except Exception as e:
                print(Fore.RED + f'{proxy} {type(e).__name__}({e})')

    async with ProxyPool(proxies) as pool:
        async with ClientSession(cookie_jar=DummyCookieJar()) as session:
            await gather(*[get_google(session, pool) for _ in range(200)])

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
    init(autoreset=True)
    run(main())
