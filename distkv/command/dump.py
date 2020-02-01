# command line interface

import sys
import asyncclick as click
from distkv.util import MsgReader
from functools import partial
from collections import Mapping

from distkv.util import MsgReader, MsgWriter
from distkv.util import yprint
from distkv.codec import unpacker

import logging

logger = logging.getLogger(__name__)


@main.group(short_help="Manage data.")  # pylint: disable=undefined-variable
@click.pass_obj
async def cli(obj):
    """
    Low-level tools that don't depend on a running server.
    """
    pass


@cli.command()
@click.argument("path", nargs=-1)
@click.pass_obj
async def cfg(obj, path):
    """Emit the current configuration as a YAML file.
    
    You can limit the output by path elements.
    E.g., "cfg connect host" will print "localhost".

    Single values are printed with a trailing line feed.
    """
    cfg = obj.cfg
    for p in path:
        try:
            cfg = cfg[p]
        except KeyError:
            if obj.debug:
                print("Unknown:",p)
            sys.exit(1)
    if isinstance(cfg,str):
        print(cfg, file=obj.stdout)
    else:
        yprint(cfg, stream=obj.stdout)


@cli.command()
@click.argument("file", nargs=1)
@click.pass_obj
async def file(obj, file):
    """Read a MsgPack file and dump as YAML."""
    async with MsgReader(path=file) as f:
        async for msg in f:
            yprint(msg, stream=obj.stdout)
            print("---", file=obj.stdout)

@cli.command()
@click.argument("node", nargs=1)
@click.argument("file", type=click.Path(), nargs=1)
@click.pass_obj
async def init(obj, node, file):
    """Write an initial preload file.
    
    Usage: distkv dump init <node> <outfile>

    Writes an initial DistKV file that behaves as if it was generated by <node>.

    Using this command, followed by "distkv server -l <outfile> <node>", is
    equivalent to running "distkv server -i 'Initial data' <node>.
    """
    async with MsgWriter(path=file) as f:
        await f(dict(chain=dict(node=node,tick=1,prev=None),depth=0,path=[],tock=1,value="Initial data"))

@cli.command()
@click.argument("path", nargs=-1)
@click.pass_obj
async def msg(obj, path):
    """
    Monitor the server-to-sever message stream.

    The default is the main server's "update" stream.
    Use '+NAME' to monitor a different stream instead.
    Use '+' to monitor all streams.

    Common streams:
    * ping: sync: all servers (default)
    * update: data changes
    * del: sync: nodes responsible for cleaning up deleted records
    """
    from distkv.backend import get_backend
    import msgpack

    px = 0
    if not path:
        path = obj.cfg.server.root.split('.')
        path.append('update')
    elif len(path) == 1:
        path = path[0].split('.')
        if len(path) == 1 and path[0].startswith('+'):
            p = path[0][1:]
            path = obj.cfg.server.root.split('.') + [p or '#']
            if not p:
                px = len(path)-1
    be = obj.cfg.server.backend
    kw = obj.cfg.server[be]

    async with get_backend(be)(**kw) as conn:
        async with conn.monitor(*path) as stream:
            async for msg in stream:
                v = vars(msg)
                if isinstance(v.get('payload'),(bytearray,bytes)):
                    t = msg.topic
                    v = unpacker(v['payload'])
                    if px > 0:
                        v['_topic'] = t[px:]
                else:
                    v['_type'] = type(msg).__name__
                yprint(v, stream=obj.stdout)
                print("---", file=obj.stdout)
