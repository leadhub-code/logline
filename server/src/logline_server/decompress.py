'''
Decompression of DATA payload bodies for the logline/2 protocol.

The agent only ever uses codecs from the Python standard library, so the
server needs nothing beyond gzip/zlib here.
'''

from asyncio import to_thread
import gzip
import zlib

from .framing import ProtocolError


async def decompress(codec, body, raw_size):
    '''
    Decompress a DATA body according to its codec and verify the result
    matches the declared uncompressed size.
    '''
    if codec == 'none':
        data = bytes(body)
    elif codec == 'gzip':
        data = await to_thread(gzip.decompress, body)
    elif codec == 'deflate':
        data = await to_thread(zlib.decompress, body)
    else:
        raise ProtocolError(f'Unsupported codec: {codec!r}')
    if len(data) != raw_size:
        raise ProtocolError(f'Decompressed size {len(data)} does not match declared raw_size {raw_size}')
    return data
