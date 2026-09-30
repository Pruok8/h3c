"""make_crt_session.py - 从 SecureCRT 的 Default.ini 生成一个带登录脚本的会话。

为什么这么做：这个 SecureCRT 8.7.2 的 crack 版**不认 `/SCRIPT` 命令行开关**
（会弹"命令行上出现意外的参数 '/SCRIPT'"）。但会话自带的"登录脚本"是支持的
（Default.ini 里就有 `D:"Use Login Script"` / `S:"Script Filename V2"`）。
于是改成：造一个 Telnet 会话，连上 127.0.0.1:30001 时自动跑我们的脚本。

按字节做替换，避免碰坏 Default.ini 里那些字体之类的二进制字段。

用法::

    python make_crt_session.py <Default.ini> <输出目录> [会话名] [脚本路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

HOST = "127.0.0.1"
TELNET_PORT = 30001
# 相对本脚本定位，保证整个 skill 目录复制到别的终端后仍然可用
SCRIPT = str(Path(__file__).resolve().parent / "crt_probe.py")


def patch(data: bytes, pairs: list[tuple[bytes, bytes]]) -> bytes:
    for old, new in pairs:
        if old not in data:
            raise SystemExit(f"源 ini 里找不到键，无法替换：{old!r}")
        data = data.replace(old, new, 1)
    return data


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    name = sys.argv[3] if len(sys.argv) > 3 else "hcl-console"
    script = sys.argv[4] if len(sys.argv) > 4 else SCRIPT
    plain = len(sys.argv) > 5 and sys.argv[5] == "plain"

    data = src.read_bytes()
    # 文档 Creating_Python_Scripts.htm 明确要求：想让会话自动跑脚本，必须
    # **关掉 Automatic logon**（= D:"Use Login Script"），再勾上 Logon script
    # （= D:"Use Script File" + 文件名）。两个都置 1 是不跑的。
    data = patch(data, [
        (b'S:"Protocol Name"=SSH2\r\n', b'S:"Protocol Name"=Telnet\r\n'),
        (b'S:"Hostname"=\r\n', f'S:"Hostname"={HOST}\r\n'.encode()),
        (b'D:"Use Login Script"=00000000\r\n', b'D:"Use Login Script"=00000000\r\n'),
        (b'D:"Use Script File"=00000000\r\n',
         b'D:"Use Script File"=0000000%d\r\n' % (0 if plain else 1)),
        (b'S:"Script Filename V2"=\r\n',
         f'S:"Script Filename V2"={"" if plain else script}\r\n'.encode()),
        # 关键：非 SSH 协议的端口键是朴素的 D:"Port"（对照 Default_RDP.ini 的
        # D:"Port"=00000d3d）。写成 D:"[Telnet] Port" 会被忽略 -> 端口回落成 23 -> 连不上。
        (b'D:"[SSH2] Port"=00000016\r\n',
         b'D:"[SSH2] Port"=00000016\r\nD:"Port"=%08x\r\n' % TELNET_PORT),
    ])

    sessions = out_dir / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    target = sessions / f"{name}.ini"
    target.write_bytes(data)

    print(f"已生成会话：{target}")
    for key in (b'S:"Protocol Name"', b'S:"Hostname"', b'D:"Port"',
                b'D:"Use Login Script"', b'D:"Use Script File"',
                b'S:"Script Filename V2"'):
        for line in data.split(b"\r\n"):
            if line.startswith(key):
                print(f"  {line.decode('latin-1')}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
