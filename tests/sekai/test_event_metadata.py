"""验证活动卡缺失、多属性与团归属的解析。"""

import ast
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
EVENT = ROOT / 'src/plugins/sekai/modules/event.py'
UNITS = ['light_sound', 'idol', 'street', 'theme_park', 'school_refusal', 'piapro']
ATTRS = ['cool', 'happy', 'mysterious', 'cute', 'pure']
CHARACTER_UNITS = {cid: UNITS[(cid - 1) // 4] if cid <= 20 else 'piapro' for cid in range(1, 27)}


class Table:
    """提供解析函数使用的异步表查询。"""

    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    async def get_sorted(self, key):
        return sorted(self.rows, key=lambda row: row[key])

    async def get_indexed(self, key):
        return {row[key]: row for row in self.rows}

    async def find_by(self, key, value, mode='first'):
        matches = [row for row in self.rows if row.get(key) == value]
        return matches if mode == 'all' else next(iter(matches), None)

    async def find_by_id(self, identifier):
        return await self.find_by('id', identifier)

    async def collect_by_ids(self, identifiers):
        indexed = {row['id']: row for row in self.rows}
        return [indexed[identifier] for identifier in identifiers if identifier in indexed]


async def supply_type(ctx, identifier):
    return ctx.supplies.get(identifier, 'normal')


def load_functions():
    """加载实际解析函数，跳过插件注册与素材访问。"""
    names = {'EventDetail', 'get_event_detail', 'get_event_banner_card', 'get_event_banner_chara_id',
             'get_ban_events_id_set', 'is_ban_event', 'get_chara_id_by_cuid', 'get_chara_ban_events'}
    tree = ast.parse(EVENT.read_text(encoding='utf-8-sig'))
    nodes = [node for node in tree.body if getattr(node, 'name', None) in names]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    namespace = dict(__name__=__name__, dataclass=dataclass, datetime=datetime, CARD_ATTRS=ATTRS,
                     EVENT_TYPE_SHOW_NAMES={'marathon': '', 'world_bloom': 'WorldLink'},
                     get_unit_by_chara_id=CHARACTER_UNITS.get, get_card_supply_type=supply_type,
                     get_character_first_nickname=str)

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    namespace['assert_and_reply'] = require
    async def prepare_assets(requests):
        return {key: await factory() for key, factory in requests.items()}
    namespace['prepare_assets'] = prepare_assets
    exec(compile(ast.fix_missing_locations(module), str(EVENT), 'exec'), namespace)
    return namespace


class EventMetadataTests(unittest.IsolatedAsyncioTestCase):
    """覆盖真实活动结构与各团的同规则总集篇。"""

    def setUp(self):
        self.functions = load_functions()
        now = datetime.now()
        self.events = [dict(id=1, name='普通箱活', eventType='marathon', assetbundleName='event_1',
                            startAt=(now - timedelta(days=10)).timestamp() * 1000,
                            aggregateAt=(now - timedelta(days=5)).timestamp() * 1000),
                       dict(id=221, name='团总集篇', eventType='marathon', assetbundleName='event_memories',
                            startAt=(now - timedelta(hours=1)).timestamp() * 1000,
                            aggregateAt=(now + timedelta(days=4)).timestamp() * 1000)]
        self.ctx = SimpleNamespace(supplies={}, md=SimpleNamespace(
            events=Table(self.events), event_cards=Table([dict(eventId=1, cardId=10)]),
            cards=Table([dict(id=10, characterId=1)]), event_musics=Table([dict(eventId=1)]),
            game_character_units=Table([dict(id=cid, gameCharacterId=cid, unit=unit) for cid, unit in CHARACTER_UNITS.items()]),
            event_deck_bonuses=Table([])))

    def set_bonuses(self, cids, attrs=ATTRS):
        self.ctx.md.event_deck_bonuses = Table(
            [dict(eventId=221, cardAttr=attr, bonusRate=25) for attr in attrs]
            + [dict(eventId=221, gameCharacterUnitId=cid, cardAttr=attr, bonusRate=75) for cid in cids for attr in attrs])

    async def detail(self):
        return await self.functions['get_event_detail'](self.ctx, 221, [])

    async def test_memory_events_for_all_six_units(self):
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.set_bonuses([cid for cid, owner in CHARACTER_UNITS.items() if owner == unit])
                detail = await self.detail()
                self.assertEqual(detail.unit, unit)
                self.assertIsNone(detail.banner_cid)
                self.assertIsNone(detail.bonus_attr)
                self.assertEqual(detail.bonus_attrs, ATTRS)
                self.assertEqual(detail.event_cards, [])
                self.assertNotIn(221, await self.functions['get_ban_events_id_set'](self.ctx))

    async def test_normal_box_keeps_its_owner(self):
        self.assertEqual(await self.functions['get_event_banner_chara_id'](self.ctx, self.events[0]), 1)
        events = await self.functions['get_chara_ban_events'](self.ctx, 1)
        self.assertEqual([(event['id'], event['ban_index']) for event in events], [(1, 1)])

    async def test_current_box_before_music_release_keeps_its_owner(self):
        self.set_bonuses([1, 2, 3, 4], ['cool'])
        self.ctx.md.event_cards.rows.append(dict(eventId=221, cardId=10))
        self.assertIn(221, await self.functions['get_ban_events_id_set'](self.ctx))
        self.assertEqual(await self.functions['get_event_banner_chara_id'](self.ctx, self.events[1]), 1)

    async def test_book_music_without_cards_has_no_owner(self):
        self.ctx.md.event_musics.rows.append(dict(eventId=221))
        self.assertIsNone((await self.detail()).banner_cid)

    async def test_missing_card_master_is_skipped(self):
        self.ctx.md.event_cards.rows.append(dict(eventId=221, cardId=999))
        self.assertIsNone(await self.functions['get_event_banner_card'](self.ctx, 221))

    async def test_festival_only_has_no_owner(self):
        self.ctx.md.event_cards.rows.append(dict(eventId=221, cardId=10))
        self.ctx.supplies[10] = 'festival_limited'
        self.assertIsNone(await self.functions['get_event_banner_card'](self.ctx, 221))

    async def test_lowest_valid_card_is_selected(self):
        self.ctx.md.event_cards.rows.extend([dict(eventId=221, cardId=999), dict(eventId=221, cardId=12), dict(eventId=221, cardId=10), dict(eventId=221, cardId=11)])
        self.ctx.md.cards.rows.extend([dict(id=11, characterId=2), dict(id=12, characterId=3)])
        self.ctx.supplies[10] = 'festival_limited'
        self.assertEqual((await self.functions['get_event_banner_card'](self.ctx, 221))['id'], 11)

    async def test_single_attribute_is_preserved(self):
        self.set_bonuses([9, 10, 11, 12], ['cool'])
        detail = await self.detail()
        self.assertEqual(detail.bonus_attr, 'cool')
        self.assertEqual(detail.bonus_attrs, ['cool'])

    async def test_multiple_attributes_have_no_single_icon(self):
        self.set_bonuses([9], ['pure', 'cool'])
        detail = await self.detail()
        self.assertIsNone(detail.bonus_attr)
        self.assertEqual(detail.bonus_attrs, ['cool', 'pure'])

    async def test_mixed_units_remain_unassigned(self):
        self.set_bonuses([1, 9])
        self.assertIsNone((await self.detail()).unit)

    async def test_associated_singer_keeps_human_unit(self):
        self.ctx.md.game_character_units.rows.append(dict(id=101, gameCharacterId=21, unit='street'))
        self.set_bonuses([9, 101])
        self.assertEqual((await self.detail()).unit, 'street')

    async def test_empty_world_bloom_has_no_first_card(self):
        self.ctx.md.events.rows[1]['eventType'] = 'world_bloom'
        detail = await self.detail()
        self.assertIsNone(detail.unit)
        self.assertIsNone(detail.banner_cid)

    async def test_dangling_event_music_is_filtered(self):
        self.ctx.md.event_musics.rows.append(dict(eventId=999))
        self.assertNotIn(999, await self.functions['get_ban_events_id_set'](self.ctx))

    async def test_thumbnail_limit_preserves_full_card_metadata(self):
        self.ctx.md.event_cards = Table([dict(eventId=1, cardId=identifier) for identifier in range(1, 21)])
        self.ctx.md.cards = Table([dict(id=identifier, characterId=1) for identifier in range(1, 21)])
        calls = []
        async def thumbnail(ctx, card, **kwargs):
            calls.append(card['id'])
            return card['id']
        self.functions['get_card_full_thumbnail'] = thumbnail
        detail = await self.functions['get_event_detail'](self.ctx, 1, ['card_thumbs'], card_thumb_limit=8)
        self.assertEqual(len(detail.event_cards), 20)
        self.assertEqual(detail.event_card_thumbs, list(range(1, 9)))
        self.assertEqual(calls, list(range(1, 9)))
        calls.clear()
        detail = await self.functions['get_event_detail'](self.ctx, 1, ['card_thumbs'])
        self.assertEqual(detail.event_card_thumbs, list(range(1, 21)))

    async def test_empty_event_thumbnail_preparation_stays_empty(self):
        detail = await self.functions['get_event_detail'](self.ctx, 221, ['card_thumbs'], card_thumb_limit=8)
        self.assertEqual(detail.event_cards, [])
        self.assertEqual(detail.event_card_thumbs, [])


if __name__ == '__main__':
    unittest.main()
