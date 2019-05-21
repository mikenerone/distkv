"""
This module's job is to run code, resp. to keep it running.


"""

import anyio
from asyncserf.actor import Actor, NodeList
from asyncserf.actor import PingEvent, TagEvent, UntagEvent, AuthPingEvent
from time import monotonic as time
from copy import deepcopy
import psutil
import time
from asyncserf.client import Serf

from .codec import packer, unpacker
from .actor import ClientActor
from .actor import DetachedState, PartialState, CompleteState, ActorState

try:
    from contextlib import asynccontextmanager
except ImportError:
    from async_generator import asynccontextmanager

from .exceptions import ServerError
from .client import AttrClientEntry, ClientRoot, ClientEntry

import logging
logger = logging.getLogger(__name__)

QLEN = 10

class NotSelected(RuntimeError):
    """
    This node has not been selected for a very long time. Something is amiss.
    """

    pass


class RunnerEntry(AttrClientEntry):
    """
    An entry representing some hopefully-running code.

    The code will run some time after ``target`` has passed.
    On success, it will run again ``repeat`` seconds later (if >0).
    On error, it will run ``delay`` seconds later (if >0), multiplied by 2**backoff.

    Arguments:
      code (tuple): The actual code that should be started.
      data (dict): Some data to go with the code.
      started (float): timestamp when the job was last started
      stopped (float): timestamp when the job last terminated
      delay (float): time before restarting the job on error
      repeat (float): time before restarting on success
      target (float): time the job should be started at
      backoff (int): how often the job terminated with an error
      result: the return value, assuming the job succeeded
      node (str): the node which is running this job, None if not executing

    The code runs with these additional keywords::
      _entry: this object
      _client: the DistKV client connection
      _info: a queue to send events to the task. A message of ``None``
        signals that the queue was overflowing and no further messages will
        be delivered.

    Messages are defined in :module:`distkv.actor`.
    """

    ATTRS = "code data started stopped result node backoff delay repeat target".split()

    started = 0  # timestamp
    stopped = 0  # timestamp
    delay = 100  # timedelta, before restarting
    backoff = 1  # how often a retry failed
    repeat = 0
    target = 0

    node = None  # on which the code is currently running
    code = None  # what to execute
    scope = None  # scope to kill off
    _comment = None  # used for error entries, i.e. mainly Cancel
    _q = None  # send events to the running task. Async tasks only.

    def __init__(self, *a, **k):
        super().__init__(*a, **k)

        self._task = None
        self.code = None  # code location
        self.data = {}  # local data

    async def run(self):
        if self.code is None:
            return  # nothing to do here
        try:
            try:
                if self.node is not None:
                    raise RuntimeError("already running on %s", self.node)
                code = self.root.code.follow(*self.code, create=False)
                data = deepcopy(self.data)

                if code.is_async:
                    data['_info'] = self._q = anyio.create_queue(QLEN)
                    if self.root._active is not None:
                        await self._q.put(self.root._active)
                data["_entry"] = self
                data["_client"] = self.root.client

                self.started = time.time()
                self.node = self.root.name

                await self.save(wait=True)
                if self.node != self.root.name:
                    raise RuntimeError("Rudely taken away from us.")

                async with anyio.open_cancel_scope() as sc:
                    self.scope = sc
                    res = code(**data)
                    if code.is_async is not None:
                        res = await res
                    await sc.cancel()
            finally:
                self.scope = None
                self._q = None
                t = time.time()

        except BaseException as exc:
            c, self._comment = self._comment, None
            await self.root.err.record_exc(
                "run", *self._path, exc=exc, data=data, comment=c
            )
            self.backoff += 1
            if self.node == self.root.name:
                self.node = None
        else:
            self.result = res
            self.backoff = 0
            self.node = None
        finally:
            self.stopped = t
            if self.backoff > 0:
                self.retry = t + (1 << self.backoff) * self.delay
            else:
                self.retry = None
            try:
                await self.save()
            except ServerError:
                logger.exception("Could not save")

    async def send_event(self, evt):
        if self._q is not None:
            if self._q.qsize() < QLEN-1:
                await self._q.put(evt)
            elif self._q.qsize() == QLEN-1:
                await self._q.put(None)
                self._q = None

    async def seems_down(self):
        self.node = None
        await self.save()

    async def set_value(self, val):
        n = self.node
        c = self.code
        await super().set_value(val)

        # Check whether running code needs to be killed off
        if self.scope is None:
            return
        if c != self.code:
            # The code changed.
            self._comment = "Cancel: Code changed"
            await self.scope.cancel()
        elif self.node == n:
            # Nothing changed.
            return
        elif n == self.root.name:
            # Owch. Our job got taken away from us.
            self._comment = "Cancel: Node set to %r" % (self.node,)
            await self.scope.cancel()
        elif n is not None:
            logger.warning(
                "Runner %s at %r: running but node is %s",
                self.root.name,
                self.subpath,
                n,
            )
        # otherwise this is the change where we took the thing

        await self.root.trigger_rescan()


    async def run_at(self, t: float):
        """Next run at this time.
        """
        self.target = t
        await self.save()

    def should_start(self, t=None):
        """Tell whether this job might want to be started.

        Returns:
          False: No, it's running (or has run and doesn't restart).
          n>0: wait for n seconds before thinking again.
          n<0: should have been started n seconds ago, do something!

        """

        if self.code is None:
            return False
        if self.node is not None:
            return False
        if t is None:
            t = time.time()

        if self.target > self.started:
            return self.target - t
        elif self.backoff:
            return self.stopped + self.delay * (1 << self.backoff) - t
        else:
            return False

    def __hash__(self):
        return hash(self.subpath)

    def __eq__(self, other):
        other = getattr(other, "subpath", other)
        return self.name == other

    def __lt__(self, other):
        other = getattr(other, "subpath", other)
        return self.name < other

    @property
    def age(self):
        return time.time() - self.started


class RunnerNode:
    """
    Represents all nodes in this runner group.
    """

    seen = 0
    load = 999

    def __new__(cls, root, name):
        try:
            self = root._nodes[name]
        except KeyError:
            self = object.__new__(cls)
            self.root = root
            self.name = name
            root._nodes[name] = self
        return self

    def __init__(self, *a, **k):
        pass


class _BaseRunnerRoot(ClientRoot):
    """common code for RunnerRoot and SingleRunnerRoot"""
    _active: ActorState = None
    _trigger: anyio.abc.Event = None
    _run_now_task: anyio.abc.CancelScope = None

    err = None
    code = None
    this_root = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._nodes = {}

    async def run_starting(self):
        from .errors import ErrorRoot
        from .code import CodeRoot

        self.err = await ErrorRoot.as_handler(self.client)
        self.code = await CodeRoot.as_handler(self.client)

        g = ["run"]
        if "name" in self._cfg:
            g.append(self._cfg["name"])
        if self.value and "name" in self.value:
            g.append(self.value["name"])
        self.group = ".".join(g)
        self.node_history = NodeList(0)
        self._start_delay = self._cfg["start_delay"]

        await super().run_starting()

    @property
    def name(self):
        """my node name"""
        return self._name

    async def running(self):
        await self._tg.spawn(self._run_actor)

    async def _run_actor(self):
        raise RuntimeError("You want to override me.""")

    async def trigger_rescan(self):
        """Tell the run_now task to rescan our job list"""
        if self._trigger is not None:
            await self._trigger.set()

    async def _run_now(self, evt = None):
        async with anyio.create_task_group() as tg:
            self._run_now_task = tg.cancel_scope
            if evt is not None:
                await evt.set()
            while True:
                self._trigger = anyio.create_event()
                d_next = 99999

                for j in self.this_root.all_children:
                    d = j.should_start()
                    if d is False:
                        continue
                    if d <= 0:
                        await self.tg.spawn(j.run)
                        await anyio.sleep(self._start_delay)
                    elif d_next > d:
                        d_next = d

                async with anyio.move_on_after(d_next):
                    await self._trigger.wait()

class RunnerRoot(_BaseRunnerRoot):
    """
    This class represents the root of a code runner. Its job is to start
    (and periodically restart, if required) the entry points stored under it.

    Config file:

    Arguments:
      path (tuple): the location this entry is stored at. Defaults to
        ``('.distkv', 'run')``.
      name (str): this runner's name. Defaults to the client's name plus
        the name stored in the root node, if any.
      actor (dict): the configuration for the underlying actor. See
        ``asyncserf.actor`` for details.
    """

    CFG = "runner"

    @classmethod
    def child_type(cls, name):
        return RunnerEntry

    def get_node(self, name):
        return RunnerNode(self, name)

    @property
    def name(self):
        """my node name"""
        return self._name

    @property
    def max_age(self):
        """Timeout after which we really should have gotten another go"""
        return self._act.cycle_time_max * (self._act.history_size + 1.5)

    @property
    def this_root(self):
        return self

    async def _run_actor(self):
        async with anyio.create_task_group() as tg:
            self.tg = tg

            async with ClientActor(self.client, self.name, prefix=self.group, tg=tg, cfg=self._cfg) as act:
                self._act = act

                self._age_q = anyio.create_queue(10)
                await self.spawn(self._age_killer)

                psutil.cpu_percent(interval=None)
                await self._act.set_value(0)
                self.seen_load = None

                async for msg in act:
                    logger.debug("MSG %r",msg)
                    if isinstance(msg, PingEvent):
                        await act.set_value(
                            100 - psutil.cpu_percent(interval=None)
                        )

                        node = self.get_node(msg.node)
                        node.load = msg.value
                        node.seen = time.time()
                        if self.seen_load is not None:
                            self.seen_load += msg.value
                        self.node_history += node

                    elif isinstance(msg, TagEvent):
                        load = 100 - psutil.cpu_percent(interval=None)
                        await act.set_value(load)
                        if self.seen_load is not None:
                            pass  # TODO

                        self.node_history += self.name
                        evt = anyio.create_event()
                        await self.spawn(self._run_now, evt)
                        await self._age_q.put(None)
                        await evt.wait()

                    elif isinstance(msg, UntagEvent):
                        await act.set_value(
                            100 - psutil.cpu_percent(interval=None)
                        )
                        self.seen_load = 0

                        await self._run_now_task.cancel()
                        # TODO if this is a DetagEvent, kill everything?
                pass # end of actor task


    async def _age_killer(self):
        t1 = time.time()
        while self._age_q is not None:
            async with anyio.move_on_after(self.max_age) as sc:
                logger.debug("T1 %f", self.max_age)
                await self._age_q.get()
                t1 = time.time()
                continue
            t2 = time.time()
            if t1 + self.max_age < t2:
                logger.debug("T3")
                raise NotSelected(self.max_age, t, time.time())
            t1 = t2

    async def _cleanup_nodes(self):
        while len(self.node_history) > 1:
            node = self.get_node(self.node_history[-1])
            if node.age < self.max_age:
                break
            assert node.name == self.node_history.pop()
            for j in self.this_root.all_children:
                if j.node == node.name:
                    await j.seems_down()


class RunnerNodeEntry(ClientEntry):
    """
    Sub-node so that a SingleRunnerRoot runs only its local nodes.
    """

    @classmethod
    def child_type(cls, name):
        return RunnerEntry


class SingleRunnerRoot(_BaseRunnerRoot):
    """
    This class represents the root of a code runner. Its job is to start
    (and periodically restart, if required) the entry points stored under it.

    While :cls:`RunnerRoot` tries to ensure that the code in question runs
    on any cluster member, this class runs tasks on a single node.
    The code is able to check whether any and/or all of the cluster's main
    nodes are reachable; this way, the code can default to local operation
    if connectivity is lost.

    Local data (dict):

    Arguments:
      cores (tuple): list of nodes whose reachability may determine
        whether the code uses local/emergency/??? mode.
      
    Config file:

    Arguments:
      path (tuple): the location this entry is stored at. Defaults to
        ``('.distkv', 'process')``.
      name (str): this runner's name. Defaults to the client's name plus
        the name stored in the root node, if any.
      actor (dict): the configuration for the underlying actor. See
        ``asyncserf.actor`` for details.
    """

    CFG = "singlerunner"

    err = None
    _act = None
    code = None

    @classmethod
    def child_type(cls, name):
        return RunnerNodeEntry

    async def set_value(self, val):
        await super().set_value(val)
        try:
            cores = val['cores']
        except (TypeError, KeyError):
            if self._act is not None:
                await self._act.disable(0)
        else:
            if self.name in cores:
                await self._act.enable(len(cores))
            else:
                await self._act.disable(len(cores))

    async def notify_active(self):
        """
        Notify all running jobs that there's a change in active status
        """
        oac = self._active
        ac = len(self.node_history)
        if ac == 0:
            ac = DetachedState
        elif self.name in self.node_history and ac == 1:
            ac = DetachedState
        elif ac >= self.n_nodes:
            ac = CompleteState
        else:
            ac = PartialState

        if oac is not ac:
            self._active = ac
            for n in self.this_root.all_children():
                await n.send_event(evt)

    async def run_starting(self):
        self.this_root = self.get(self.name)
        await super().run_starting()

    @property
    def max_age(self):
        """Timeout after which we really should have gotten another ping"""
        return self._act.cycle_time_max * 1.5

    async def _run_actor(self):
        async with anyio.create_task_group() as tg:
            self.tg = tg
            self._age_q = anyio.create_queue(1)

            async with ClientActor(self.client, self.name, prefix=self.group, tg=tg, cfg=self._cfg) as act:
                self._act = act
                await tg.spawn(self._age_notifier)
                await self.spawn(self._run_now)
                await self._act.set_value(0)

                async for msg in act:
                    if isinstance(msg, AuthPingEvent):
                        self.node_history += msg.node
                        await self._age_q.put(None)
                        await self.notify_active()

                pass # end of actor task

    async def _age_notifier(self):
        while self._age_q is not None:
            flag = False
            async with anyio.move_on_after(self.max_age):
                await self._age_q.get()
                flag = True
            if not flag:
                await self.notify_active()

