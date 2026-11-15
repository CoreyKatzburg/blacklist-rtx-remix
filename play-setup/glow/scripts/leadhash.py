"""Port of BlacklistExplorer.Binary.Hash (CRC-32/MPEG-2 style, lowercase ASCII, final xor)."""


def lead_hash(text: str) -> int:
    num = 0
    for b in text.lower().encode('ascii'):
        num ^= (b << 24) & 0xFFFFFFFF
        for _ in range(8):
            num = ((num << 1) & 0xFFFFFFFF) ^ (0x04C11DB7 if num & 0x80000000 else 0)
    return num ^ 0xFFFFFFFF


def hx(text: str) -> str:
    return '%08X' % lead_hash(text)


if __name__ == '__main__':
    import sys
    for a in sys.argv[1:]:
        print(hx(a), a)
