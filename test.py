from __future__ import annotations

import json
import re
from base64 import b64decode
from collections import defaultdict
from heapq import heappush
from typing import Any, Final, Iterable, Self
from urllib.parse import unquote


class Proxy:
    def __init__(
        self, server_addr: str, encrypt_method: str, password: str, name: str = 'UNKNOWN'
    ) -> None:
        self._server_addr: Final[str] = server_addr
        self._encrypt_method: Final[str] = encrypt_method
        self._password: Final[str] = password
        self.name: Final[str] = name
        self.id: Final[tuple[str, int, str]]
        if m := re.search(r'(.*?)(\d+)线 \| ([A-Z])', self.name):
            self.id = (m.group(1), int(m.group(2)), m.group(3))
        else:
            self.id = ('UNKNOWN', 0, 'UNKNOWN')

    @classmethod
    def from_base64(cls, encode: str) -> list[Self]:
        """从 base64 中解析节点"""
        result: list[Self] = list()
        for line in b64decode(encode).decode().splitlines():
            if m := re.search(r'^ss://([A-Za-z0-9]+)@([a-z0-9\.]+?\.com:\d{1,5})#(.*?)$', line):
                encrypt_method, password = b64decode(m.group(1)).decode().split(':')
                server_addr = m.group(2)
                name = unquote(m.group(3))
                if name.startswith('套餐到期') or name.startswith('剩余流量'):
                    continue
                result.append(
                    cls(
                        server_addr=server_addr,
                        encrypt_method=encrypt_method,
                        password=password,
                        name=name,
                    )
                )
        return result

    @classmethod
    def groupby(cls, proxies: Iterable[Self]) -> dict[str, list[Self]]:
        """将若干节点按 id[0] 分组，各组内按 id[1], id[2] 排序"""
        result: dict[str, list[Self]] = defaultdict(list)

        for p in proxies:
            heappush(result[p.id[0]], p)

        return result

    def __repr__(self) -> str:
        return f'{type(self).__name__}(name="{self.name}", server_addr="{self._server_addr}", encrypt_method="{self._encrypt_method}", password="{self._password}")'

    def __str__(self) -> str:
        return f'name={self.name}, server_addr="{self._server_addr}"'

    def __hash__(self) -> int:
        return hash(repr(self))

    def __eq__(self, o: Any) -> bool:
        return isinstance(o, type(self)) and hash(self) == hash(o)

    def __gt__(self, o: Self) -> bool:
        return self.id > o.id

    def __lt__(self, o: Self) -> bool:
        return self.id < o.id


def main():
    with open('./temp/7c151fe3.txt', 'r') as fp:
        source = fp.read()
    proxies = Proxy.from_base64(source)
    # for p in proxies:
    #     print(p)

    proxy_group = Proxy.groupby(proxies)
    print(json.dumps(proxy_group, indent=2, ensure_ascii=False, default=lambda _: str(_)))

    # for k, v in proxy_group.items():
    #     print(k)
    #     for p in v:
    #         print(repr(p))
    #     print()


if __name__ == '__main__':
    main()
