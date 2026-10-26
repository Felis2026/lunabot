"""验证生日领取状态、下一轮进度与不同来源库存的时间选择。"""

import ast
import copy
import math
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULES = ROOT / 'src/plugins/sekai/modules'
NOW = 1_792_818_900_000
PARTY = dict(id=2, deliveryItemMaterialId=176, deliveryRewardMysekaiMaterialId=69)


class Widget:
    """承接面板排版调用，供测试检查实际输出文字和进度。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: self


def load_functions():
    """加载实际计算和面板函数，跳过插件注册、字体与素材加载。"""
    source = ast.parse((MODULES / 'birthday.py').read_text(encoding='utf-8-sig'))
    names = {'_birthday_find', '_birthday_inventory', '_birthday_calculate', '_draw_birthday_progress_panel'}
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    profile = ast.parse((MODULES / 'profile.py').read_text(encoding='utf-8-sig'))
    nodes.insert(0, next(node for node in profile.body if isinstance(node, ast.FunctionDef)
                         and node.name == 'to_unix_millis'))
    texts, bars = [], []
    namespace = dict(BIRTHDAY_DELIVERY_BASE_POINT=100, math=math, RED='red',
                     VSplit=Widget, HSplit=Widget, Frame=Widget,
                     ImageBox=lambda *args, **kwargs: Widget(),
                     TextBox=lambda text, *args: texts.append(text) or Widget(),
                     _birthday_style=lambda *args, **kwargs: None,
                     _draw_birthday_progress=lambda value, total: bars.append((value, total)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MODULES / 'birthday.py'), 'exec'), namespace)
    return namespace, texts, bars


class BirthdayProgressTests(unittest.TestCase):
    def setUp(self):
        self.functions, self.texts, self.bars = load_functions()
        self.capture = dict(updatedResources=dict(now=NOW,
            userBirthdayParties=[dict(birthdayPartyId=2, deliveryTotalPoint=10100,
                                     droppedMysekaiMaterialCount=1, obtainedMysekaiMaterialCount=0)],
            userMaterials=[dict(materialId=176, quantity=94)],
            userMysekaiMaterials=[dict(mysekaiMaterialId=69, quantity=1)]))

    def calculate(self, **kwargs):
        """按本期一万分门槛调用实际计算函数。"""
        return self.functions['_birthday_calculate'](PARTY, [], 10000, 25,
            kwargs.get('suite'), kwargs.get('capture', self.capture),
            kwargs.get('prestart', False), kwargs.get('top', False))

    def draw(self, state, *, flower=False):
        """收集实际面板输出，检查进度条和待领取提示。"""
        self.functions['_draw_birthday_progress_panel']({'state': state}, None, None, flower)

    def test_unclaimed_flower_does_not_lock_next_round_progress(self):
        state = self.calculate()
        self.assertEqual(state['count'], 0)
        self.assertEqual(state['pending_flowers'], 1)
        self.draw(state)
        self.assertEqual(self.bars, [(100, 10000)])
        self.assertTrue(any(text.endswith('（待领取 1 朵）') for text in self.texts))
        self.assertEqual(len(self.texts), 3)
        self.assertTrue(any('1.0%' in text for text in self.texts))

    def test_gather_updates_claimed_count_and_clears_pending_without_resetting_progress(self):
        self.capture['updatedResources']['userBirthdayParties'][0].update(
            droppedMysekaiMaterialCount=0, obtainedMysekaiMaterialCount=1)
        state = self.calculate()
        self.assertEqual(state['count'], 1)
        self.draw(state)
        self.assertEqual(self.bars, [(100, 10000)])
        self.assertFalse(any('待领取' in text for text in self.texts))

    def test_exact_threshold_starts_next_round_and_keeps_pending_visible(self):
        self.capture['updatedResources']['userBirthdayParties'][0]['deliveryTotalPoint'] = 10000
        self.draw(self.calculate())
        self.assertEqual(self.bars, [(0, 10000)])
        self.assertTrue(any(text.endswith('（待领取 1 朵）') for text in self.texts))

    def test_multiple_pending_flowers_keep_actual_claimed_count(self):
        self.capture['updatedResources']['userBirthdayParties'][0].update(
            deliveryTotalPoint=20300, droppedMysekaiMaterialCount=2)
        state = self.calculate()
        self.assertEqual(state['count'], 0)
        self.draw(state)
        self.assertEqual(self.bars, [(300, 10000)])
        self.assertTrue(any(text.endswith('（待领取 2 朵）') for text in self.texts))

    def test_claimed_panel_uses_actual_claimed_count(self):
        self.draw(self.calculate(), flower=True)
        self.assertEqual(self.bars, [(0, 25)])

    def test_missing_progress_stays_unknown_and_planning_starts_at_zero(self):
        unknown = self.calculate(capture=None)
        self.assertIsNone(unknown['point'])
        self.assertIsNone(unknown['count'])
        self.draw(unknown)
        self.assertEqual(self.bars, [])
        planned = self.calculate(prestart=True)
        self.assertEqual((planned['point'], planned['count'], planned['pending_flowers']), (0, 0, 0))

    def test_newer_capture_stock_overrides_suite_and_preserves_zero(self):
        suite = dict(upload_time=NOW - 1000,
                     userMaterials=[dict(materialId=176, quantity=96)],
                     userMysekaiMaterials=[dict(mysekaiMaterialId=69, quantity=0)])
        state = self.calculate(suite=suite)
        self.assertEqual((state['inventory'], state['flower_stock']), (94, 1))
        self.capture['updatedResources']['userMysekaiMaterials'][0]['quantity'] = 0
        suite['userMysekaiMaterials'][0]['quantity'] = 3
        self.assertEqual(self.calculate(suite=suite)['flower_stock'], 0)

    def test_per_material_timestamp_wins_over_newer_unrelated_table_update(self):
        suite = dict(upload_time=NOW + 1000, userMysekaiMaterials=[dict(mysekaiMaterialId=69, quantity=3)])
        self.capture['resource_updated_at_ms'] = {'userMysekaiMaterials': NOW + 2000,
                                                'userMysekaiMaterials:69': NOW}
        self.assertEqual(self.calculate(suite=suite)['flower_stock'], 3)
        self.capture['resource_updated_at_ms']['userMysekaiMaterials:69'] = NOW + 3000
        self.assertEqual(self.calculate(suite=suite)['flower_stock'], 1)

    def test_missing_capture_inventory_falls_back_without_mutating_sources(self):
        suite = dict(userMaterials=[dict(materialId=176, quantity=96)])
        before = copy.deepcopy(suite)
        state = self.calculate(suite=suite, capture=None)
        self.assertEqual(state['inventory'], 96)
        self.assertIsNone(state['flower_stock'])
        self.assertEqual(suite, before)


if __name__ == '__main__':
    unittest.main()
