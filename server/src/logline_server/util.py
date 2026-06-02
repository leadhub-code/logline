from asyncio import to_thread


async def decompress_zst(compressed_data):
    assert isinstance(compressed_data, bytes)
    try:
        # https://python-zstandard.readthedocs.io/
        import zstandard
        return await to_thread(zstandard.decompress, compressed_data)
    except ImportError:
        pass
    try:
        # https://github.com/sergey-dryabzhinsky/python-zstd
        # https://packages.debian.org/bullseye/python3-zstd
        import zstd
        return await to_thread(zstd.decompress, compressed_data)
    except ImportError:
        pass
    raise Exception('Zstandard decompression is not available - please install zstandard or zstd')
