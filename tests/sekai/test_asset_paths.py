"""验证新旧解包路径、业务 JSON 识别与本地文件版本的一致性。"""
import ast
import asyncio
from contextvars import ContextVar
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[2]


def load_manager():
    """加载实际资源路径方法，隔离插件注册和上游服务。"""
    path = ROOT / 'src/plugins/sekai/asset.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RegionRipAssetManger')
    names = {'_iter_local_cache_paths', 'get_local_asset_cache_path', '_read_cached_asset',
             '_read_cached_json', '_get_validated_json', '_write_cached_asset', 'json'}
    source.body = [node for node in source.body if getattr(node, 'name', '') in names]
    async def pooled(callback, *args):
        return await asyncio.to_thread(callback, *args)
    namespace = dict(Path=Path, time=time, os=os, datetime=datetime, threading=threading, asyncio=asyncio,
        loads_json=json.loads, pjoin=os.path.join, create_parent_folder=lambda file: Path(file).parent.mkdir(parents=True, exist_ok=True),
        run_in_pool_until_complete=pooled, get_cfg_or_value=lambda value: value, logger=Mock(),
        DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG=None, ASSET_PREPARE_IN_POOL=ContextVar('test_json_pool', default=False))
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, source], type_ignores=[])), str(path), 'exec'), namespace)
    return namespace['RegionRipAssetManger']()


class AssetPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = load_manager()
        self.manager.cache_dir = str(self.root)
        self.manager.region = 'jp'
        self.manager.sources = []
        self.manager._json_requests = {}

    def write(self, relative, data):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def test_gacha_prefers_current_export(self):
        legacy = 'gacha/ab_test/screen/texture/bg_test.png'
        current = self.write('gacha/ab_test/screen/bg_test.png', b'current')
        self.write(legacy, b'old')
        self.assertEqual(self.manager._read_cached_asset(legacy, None), b'current')
        self.assertEqual(Path(self.manager.get_local_asset_cache_path(legacy)), current)

    def test_gacha_legacy_export_is_still_readable(self):
        legacy = self.write('gacha/ab_test/screen/texture/bg_test.png', b'legacy')
        self.assertEqual(Path(self.manager.get_local_asset_cache_path('gacha/ab_test/screen/bg_test.png')), legacy)

    def test_other_texture_directories_keep_their_meaning(self):
        requested = 'mysekai/site/sitemap/texture_rip/map.png'
        actual = self.write('mysekai/site/sitemap/texture/map.png', b'map')
        self.write('mysekai/site/sitemap/map.png', b'unrelated')
        self.assertEqual(Path(self.manager.get_local_asset_cache_path(requested)), actual)

    def test_json_extension_and_directory_are_combined(self):
        target = self.write('event_story/test/scenario/chapter.json', b'{"ScenarioId":"test"}')
        requested = 'event_story/test/scenario_rip/chapter.asset'
        self.assertEqual(Path(self.manager.get_local_asset_cache_path(requested)), target)
        self.assertEqual(self.manager._read_cached_asset(requested, None), target.read_bytes())

    def test_legacy_asset_extension_is_preserved(self):
        target = self.write('character/member/test_rip/chapter.asset', b'legacy')
        requested = 'character/member/test_rip/chapter.asset'
        self.assertEqual(Path(self.manager.get_local_asset_cache_path(requested)), target)

    def test_score_txt_is_used_by_extensionless_request(self):
        target = self.write('music/music_score/0001_01/expert.txt', b'#BPM01:120')
        self.assertEqual(Path(self.manager.get_local_asset_cache_path('music/music_score/0001_01_rip/expert')), target)

    def test_legacy_extensionless_score_is_preserved(self):
        target = self.write('music/music_score/0001_01_rip/expert', b'#BPM01:120')
        self.assertEqual(Path(self.manager.get_local_asset_cache_path('music/music_score/0001_01_rip/expert')), target)

    def test_expiry_applies_to_all_local_aliases(self):
        target = self.write('event_story/test/scenario/chapter.json', b'{}')
        os.utime(target, (time.time() - 100, time.time() - 100))
        self.assertIsNone(self.manager.get_local_asset_cache_path('event_story/test/scenario_rip/chapter.asset', 60))
        self.assertIsNone(self.manager._read_cached_asset('event_story/test/scenario_rip/chapter.asset', 60))

    def test_current_image_and_role_fingerprint_use_same_file(self):
        requested = 'gacha/ab_test/screen/texture/bg_gacha1_1.png'
        target = self.write('gacha/ab_test/screen/bg_gacha1_1.png', b'current')
        self.write(requested, b'old')
        tree = ast.parse((ROOT / 'src/plugins/sekai/modules/gacha.py').read_text(encoding='utf-8-sig'))
        function = next(node for node in tree.body if getattr(node, 'name', '') == 'costume_image_role_key')
        namespace = dict(Path=Path)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<costume-role>', 'exec'), namespace)
        ctx = Mock(region='jp', rip=self.manager)
        key = namespace['costume_image_role_key'](ctx, {'id': 1, 'assetbundleName': 'ab_test'}, 'gacha/ab_test/screen/texture', 1)
        self.assertEqual(key[2], str(target.resolve()))
        self.assertEqual(key[3], len(self.manager._read_cached_asset(requested, None)))

    async def test_json_selects_business_object_by_fields(self):
        self.write('mysekai/system/reaction/reaction.json', b'{"m_Container":{}}')
        self.write('mysekai/system/reaction/reaction__dup2.json', b'{"m_ClassName":"test"}')
        self.write('mysekai/system/reaction/reaction__dup9.json', b'{"FixturerRactions":[]}')
        self.manager.get_asset = AsyncMock(side_effect=AssertionError('不应请求上游'))
        result = await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',))
        self.assertEqual(result, {'FixturerRactions': []})
        self.manager.get_asset.assert_not_awaited()

    async def test_json_missing_cache_requests_upstream_once(self):
        self.manager.get_asset = AsyncMock(return_value=b'{"FixturerRactions":[]}')
        result = await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',))
        self.assertEqual(result, {'FixturerRactions': []})
        self.manager.get_asset.assert_awaited_once()
        self.assertEqual(json.loads((self.root / 'mysekai/system/reaction/reaction.json').read_bytes()), result)
        await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',))
        self.manager.get_asset.assert_awaited_once()

    async def test_ambiguous_json_candidates_use_upstream_once(self):
        self.write('mysekai/system/reaction/reaction__dup2.json', b'{"FixturerRactions":[1]}')
        self.write('mysekai/system/reaction/reaction__dup3.json', b'{"FixturerRactions":[2]}')
        self.manager.get_asset = AsyncMock(return_value=b'{"FixturerRactions":[3]}')
        result = await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',))
        self.assertEqual(result, {'FixturerRactions': [3]})
        self.manager.get_asset.assert_awaited_once()

    async def test_json_wrong_shape_returns_default_without_saving(self):
        self.manager.get_asset = AsyncMock(return_value=b'{"m_Container":{}}')
        result = await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',))
        self.assertIsNone(result)
        self.assertFalse((self.root / 'mysekai/system/reaction/reaction.json').exists())

    async def test_json_cold_callers_share_request_and_survive_cancellation(self):
        async def download(*args, **kwargs):
            await asyncio.sleep(.03)
            return b'{"FixturerRactions":[1]}'
        self.manager.get_asset = AsyncMock(side_effect=download)
        tasks = [asyncio.create_task(self.manager.json('mysekai/system/reaction_rip/reaction.asset',
            required_keys=('FixturerRactions',))) for _ in range(4)]
        await asyncio.sleep(.01)
        tasks[0].cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(results[1:], [{'FixturerRactions': [1]}] * 3)
        self.manager.get_asset.assert_awaited_once()
        results[1]['FixturerRactions'].append(2)
        self.assertEqual(results[2]['FixturerRactions'], [1])
        self.assertEqual(self.manager._json_requests, {})

    async def test_json_wrong_shape_raises_when_required(self):
        self.manager.get_asset = AsyncMock(return_value=b'{"m_Container":{}}')
        with self.assertRaises(Exception):
            await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',), allow_error=False)

    async def test_forced_json_refresh_skips_local_and_does_not_save(self):
        target = self.write('mysekai/system/reaction/reaction.json', b'{"FixturerRactions":[1]}')
        self.manager.get_asset = AsyncMock(return_value=b'{"FixturerRactions":[2]}')
        result = await self.manager.json('mysekai/system/reaction_rip/reaction.asset', required_keys=('FixturerRactions',), use_cache=False)
        self.assertEqual(result, {'FixturerRactions': [2]})
        self.manager.get_asset.assert_awaited_once()
        self.assertEqual(json.loads(target.read_bytes()), {'FixturerRactions': [1]})


if __name__ == '__main__':
    unittest.main()
