"""验证资源准备的保序、并发上限和取消收尾。"""
import ast
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_functions():
    """从实际源码加载纯调度函数，跳过插件注册。"""
    namespace = dict(asyncio=asyncio, asynccontextmanager=asynccontextmanager, time=time,
        ASSET_REQUEST_LIMIT=ContextVar('test_asset_limit', default=None),
        ASSET_PREPARE_IN_POOL=ContextVar('test_asset_pool', default=False),
        ASSET_RESOURCE_LIMIT=asyncio.Semaphore(4), record_asset_timing=lambda *args: None)
    for relative, names in (
        ('utils/utils.py', {'bounded_map'}),
        ('sekai/asset.py', {'resource_prepare_scope', 'prepare_assets', 'asset_request_slot'}),
    ):
        path = ROOT / 'src/plugins' / relative
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        nodes = [node for node in tree.body if getattr(node, 'name', None) in names]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), 'exec'), namespace)
    return namespace


class ResourcePreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.functions = load_functions()

    async def test_result_order_matches_input(self):
        async def work(number):
            await asyncio.sleep((5 - number) * .001)
            return number
        result = await self.functions['bounded_map'](work, range(5))
        self.assertEqual(result, list(range(5)))

    async def test_empty_input_does_not_start_work(self):
        async def fail(number):
            raise AssertionError('空输入不应执行工厂')
        self.assertEqual(await self.functions['bounded_map'](fail, []), [])

    async def test_invalid_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.functions['bounded_map'](None, [], concurrency=0)

    async def test_worker_limit_is_four(self):
        active, maximum = 0, 0
        async def work(number):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(.002)
            active -= 1
        await self.functions['bounded_map'](work, range(20))
        self.assertEqual(maximum, 4)
        self.assertEqual(active, 0)

    async def test_factory_failure_reclaims_siblings(self):
        finished = []
        async def work(number):
            try:
                if number == 0:
                    await asyncio.sleep(.002)
                    raise ValueError('expected')
                await asyncio.sleep(.02)
            finally:
                finished.append(number)
        with self.assertRaises(ValueError):
            await self.functions['bounded_map'](work, range(20))
        self.assertEqual(sorted(finished), [0, 1, 2, 3])

    async def test_parent_cancellation_waits_for_cleanup(self):
        finished = []
        started = []
        ready = asyncio.Event()
        async def work(number):
            try:
                started.append(number)
                if len(started) == 4:
                    ready.set()
                await asyncio.sleep(1)
            finally:
                await asyncio.sleep(.002)
                finished.append(number)
        task = asyncio.create_task(self.functions['bounded_map'](work, range(20)))
        await ready.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(finished), [0, 1, 2, 3])

    async def test_resource_context_is_restored(self):
        async def work():
            self.assertIs(self.functions['ASSET_REQUEST_LIMIT'].get(), self.functions['ASSET_RESOURCE_LIMIT'])
            self.assertTrue(self.functions['ASSET_PREPARE_IN_POOL'].get())
            return 7
        self.assertEqual(await self.functions['prepare_assets']({'image': work}), {'image': 7})
        self.assertIsNone(self.functions['ASSET_REQUEST_LIMIT'].get())
        self.assertFalse(self.functions['ASSET_PREPARE_IN_POOL'].get())

    async def test_four_queries_share_download_limit(self):
        active, maximum = 0, 0
        async def work():
            nonlocal active, maximum
            async with self.functions['asset_request_slot']():
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(.002)
                active -= 1
        async def query():
            return await self.functions['prepare_assets']({number: work for number in range(8)})
        await asyncio.gather(*(query() for _ in range(4)))
        self.assertEqual(maximum, 4)

    async def test_nested_preparation_does_not_deadlock(self):
        async def work():
            return await self.functions['prepare_assets']({'inner': lambda: asyncio.sleep(0, result=1)})
        result = await asyncio.wait_for(self.functions['prepare_assets']({'outer': work}), 1)
        self.assertEqual(result, {'outer': {'inner': 1}})


if __name__ == '__main__':
    unittest.main()
