import asyncio
import inspect
from asyncio import AbstractEventLoop
from asyncio.events import get_event_loop
from collections import deque
from concurrent.futures._base import Executor
from functools import partial, wraps
from multiprocessing import cpu_count
import threading
from types import MappingProxyType

from .iterator_wrapper import IteratorWrapper


class ThreadPoolException(RuntimeError):
    pass


class ThreadPoolExecutor(Executor):
    __slots__ = (
        '__loop', '__futures', '__running', '__pool', '__tasks',
        '__read_event', '__read_lock', '__write_lock',
    )

    def __init__(self, max_workers=max((cpu_count(), 2)),
                 loop: AbstractEventLoop = None):

        self.__loop = loop or get_event_loop()
        self.__futures = set()
        self.__running = True

        self.__pool = set()
        self.__tasks = deque()
        self.__read_event = threading.Event()
        self.__read_lock = threading.Lock()
        self.__write_lock = threading.RLock()

        for idx in range(max_workers):
            thread = threading.Thread(
                target=self._in_thread,
                name="[%d] Thread Pool" % idx,
                daemon=True,
            )

            thread.daemon = True

            self.__pool.add(thread)
            # Starting the thread only after thread-pool will be started
            self.__loop.call_soon(thread.start)

        self.__pool = frozenset(self.__pool)

    @staticmethod
    def _set_result(future, result, exception):
        if future.done():
            return

        if exception:
            future.set_exception(exception)
            return

        future.set_result(result)

    def _execute(self, func, future):
        if future.done():
            return

        result, exception = None, None

        if self.__loop.is_closed():
            raise asyncio.CancelledError

        try:
            result = func()
        except Exception as e:
            exception = e

        if self.__loop.is_closed():
            raise asyncio.CancelledError

        self.__loop.call_soon_threadsafe(
            self._set_result,
            future,
            result,
            exception,
        )

    def _in_thread(self):
        while self.__running and not self.__loop.is_closed():
            try:
                func, future = self.__tasks.popleft()
                self._execute(func, future)
            except IndexError:
                with self.__read_lock:
                    if self.__read_event.is_set():
                        self.__read_event.clear()

                self.__read_event.wait(timeout=1)
            except asyncio.CancelledError:
                break

    def submit(self, fn, *args, **kwargs):
        with self.__write_lock:
            task_count = len(self.__tasks)

            future = self.__loop.create_future()     # type: asyncio.Future
            self.__futures.add(future)
            future.add_done_callback(self.__futures.remove)

            self.__tasks.append((partial(fn, *args, **kwargs), future))

            if task_count == 0 and not self.__read_event.is_set():
                self.__read_event.set()

            return future

    def shutdown(self, wait=True):
        self.__running = False

        for f in filter(lambda x: not x.done(), self.__futures):
            f.set_exception(ThreadPoolException("Pool closed"))

    def __del__(self):
        self.shutdown()


def run_in_executor(func, executor=None, args=(),
                    kwargs=MappingProxyType({})) -> asyncio.Future:

    loop = get_event_loop()
    # noinspection PyTypeChecker
    return loop.run_in_executor(executor, partial(func, *args, **kwargs))


def threaded(func):
    @wraps(func)
    async def wrap(*args, **kwargs):
        future = run_in_executor(func=func, args=args, kwargs=kwargs)
        try:
            return await future
        except asyncio.CancelledError as e:
            if not future.done():
                future.set_exception(e)
            raise

    if inspect.isgeneratorfunction(func):
        return threaded_iterable(func)

    return wrap


def threaded_iterable(func=None, max_size: int = 0):
    if isinstance(func, int):
        return partial(threaded_iterable, max_size=func)
    if func is None:
        return partial(threaded_iterable, max_size=max_size)

    @wraps(func)
    def wrap(*args, **kwargs):
        return IteratorWrapper(
            partial(func, *args, **kwargs),
            max_size=max_size,
        )

    return wrap
