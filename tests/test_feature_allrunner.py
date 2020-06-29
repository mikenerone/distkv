import pytest
import trio
import anyio
import time

from .mock_mqtt import stdtest

from .run import run

from distkv.code import CodeRoot
from distkv.runner import AllRunnerRoot
from distkv.errors import ErrorRoot
from distkv.util import P
import logging

logger = logging.getLogger(__name__)


@pytest.mark.trio
async def test_83_run(autojump_clock):  # pylint: disable=unused-argument
    async with stdtest(args={"init": 123}, tocks=50) as st:
        s, = st.s
        h = p = None  # pylint
        for h, p, *_ in s.ports:
            if h[0] != ":":
                break
        async with st.client() as c:
            await ErrorRoot.as_handler(c)
            cr = await CodeRoot.as_handler(c)
            r = await AllRunnerRoot.as_handler(c, subpath=(), code=cr)
            c._test_evt = anyio.create_event()
            await cr.add(
                P("forty.two"),
                code="""\
                import trio
                c=_client
                await c._test_evt.set()
                await trio.sleep(10)
                return 42
                """,
                is_async=True,
            )
            ru = r.follow(P("foo.test"), create=True)
            ru.code = P("forty.two")
            await ru.run_at(time.time() + 1)
            logger.info("Start sleep")
            with trio.fail_after(60):
                await c._test_evt.wait()
            await run(
                "-vvv", "client", "-h", h, "-p", p, "data", "get", "-rd_", ":", do_stdout=False
            )
            await trio.sleep(11)

            logger.info("End sleep")

            r = await run("-vvv", "client", "-h", h, "-p", p, "data", "get", ":")
            assert r.stdout == "123\n"

            rs = ru.state
            assert rs.started > 0
            assert rs.stopped > 0
            assert rs.backoff == 0
            assert rs.result == 42
