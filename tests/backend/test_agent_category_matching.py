"""口语原意与目录候选分离；真实SQL仅使用额外随机测试库。"""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import update as sql_update

from agent.schemas import ConversationState
from agent.tools.group_buys import GroupBuyTools
from backend.app.models import Voucher
from backend.app.schemas.group_buys import Requirements, SearchInput
from backend.app.services import group_buys
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules
from tests.backend.test_group_buy_assistant import NOW, item, needs
from tests.backend.test_order_migration import migration_database as migration_database


def tools_for(category="炒菜", matching="semantic"):
    return GroupBuyTools(
        None,
        ConversationState(
            requirements=Requirements(
                category=category,
                category_matching=matching,
                area="南开区",
                budget_fen=20000,
            )
        ),
        AsyncMock(),
        user_message="想吃炒菜",
    )


def observe(tools, literal=False):
    tools.remember_catalog(
        {
            "options": [{"category": "天津菜"}, {"category": "家常菜"}],
            "literal_catalog_match": literal,
            "category_expression": tools.state.requirements.category,
        }
    )


async def test_related_mapping_preserves_original_hard_conditions_and_is_not_user_relaxation():
    tools = tools_for()
    observe(tools)
    original = tools.state.requirements.model_copy(deep=True)
    assert tools.apply_category_matches(["家常菜", "天津菜"]) is None
    current = tools.state.requirements
    assert current.category == original.category == "炒菜"
    assert current.area == original.area and current.budget_fen == original.budget_fen
    assert current.category_matches == ["家常菜", "天津菜"]
    assert tools.state.category_mapping.certainty == "related_only"
    assert tools.state.category_mapping.source == "observed_catalog"
    assert tools.state.goal_revision == 1
    await tools.execute(
        "update_goal",
        json.dumps(
            {
                "changes": [
                    {"field": "category", "strength": "required", "value": "炒菜"},
                ]
            }
        ),
    )
    assert tools.state.requirements.category_matches == current.category_matches
    assert tools.state.goal_revision == 1 and tools.state.category_mapping is not None
    await tools.execute(
        "update_goal",
        json.dumps(
            {
                "changes": [
                    {"field": "category", "strength": "required", "value": "天津菜"},
                ]
            }
        ),
    )
    assert tools.state.requirements.category_matches == [] and tools.state.category_mapping is None


@pytest.mark.parametrize(
    "mode,known,requested,expected",
    [
        ("literal", False, ["天津菜"], "CATEGORY_MAPPING_FORBIDDEN"),
        ("semantic", True, ["天津菜"], "LITERAL_CATEGORY_EXISTS"),
        ("semantic", False, ["未知类目"], "UNSEEN_CATEGORY"),
        ("semantic", False, ["天津菜", "天津菜"], "UNSEEN_CATEGORY"),
    ],
)
def test_mapping_rejects_literal_known_and_unobserved_categories_without_mutation(
    mode,
    known,
    requested,
    expected,
):
    tools = tools_for(matching=mode)
    observe(tools, known)
    before = tools.state.model_dump()
    assert tools.apply_category_matches(requested)["error"] == expected
    assert tools.state.model_dump() == before


def test_catalog_observation_must_match_current_scope_and_original_expression():
    tools = tools_for()
    assert tools.apply_category_matches(["天津菜"])["error"] == "CATALOG_REQUIRED"
    observe(tools)
    tools.state.requirements.budget_fen = 5000
    assert tools.apply_category_matches(["天津菜"])["error"] == "CATALOG_REQUIRED"
    assert tools.state.requirements.category_matches == []


def test_related_category_remains_unknown_with_other_usage_constraints_fully_satisfied():
    need = needs().model_copy(
        update={
            "category": "炒菜",
            "category_matching": "semantic",
            "category_matches": ["天津菜"],
        }
    )
    result = group_buys.evaluate(item(category="天津菜"), need, NOW)
    assert result.verdict == "unknown" and not result.reasons
    assert [issue.code for issue in result.issues] == ["CATEGORY_RELATED_UNVERIFIED"]
    assert result.issues[0].source == "verification"
    wrong = group_buys.evaluate(item(category="火锅"), need, NOW)
    assert wrong.verdict == "ineligible" and "品类不符合要求" in wrong.reasons


@pytest.mark.parametrize(
    "payload",
    [
        {"category": "炒菜", "category_matches": ["天津菜"]},
        {"category_matching": "semantic", "category_matches": ["天津菜"]},
        {
            "category": "炒菜",
            "category_matching": "semantic",
            "category_matches": ["天津菜", "天津菜"],
        },
    ],
)
def test_invalid_mapping_metadata_is_rejected_and_old_json_keeps_legacy_behavior(payload):
    with pytest.raises(ValidationError):
        Requirements.model_validate(payload)
    old = ConversationState.model_validate({"requirements": {"category": "火锅"}})
    assert (
        old.requirements.category_matching == "legacy" and old.requirements.category_matches == []
    )


async def test_old_goal_tool_cannot_inject_mapping_fields():
    tools = tools_for()
    response = await tools.execute(
        "set_requirements",
        '{"category":"炒菜","category_matching":"semantic",'
        '"category_matches":["天津菜"],"evidence":"想吃炒菜"}',
    )
    assert (
        response["error"] == "INVALID_ARGUMENTS" and tools.state.requirements.category_matches == []
    )


async def test_failed_goal_update_blocks_old_scope_until_corrected():
    tools = tools_for()
    before = tools.state.model_dump()
    with pytest.raises(ValidationError):
        await tools.execute(
            "update_goal",
            json.dumps(
                {
                    "changes": [
                        {
                            "field": "category",
                            "value": "炒菜",
                            "strength": "required",
                            "matching": "literal",
                        },
                        {"field": "notes", "value": "不要其他类目", "strength": "required"},
                    ]
                }
            ),
        )
    assert tools.state.model_dump() == before
    assert (await tools.execute("search_group_buys", "{}"))["error"] == "GOAL_UPDATE_REQUIRED"
    assert (await tools.execute("finish", '{"answer":"已按新条件查询"}'))[
        "error"
    ] == "GOAL_UPDATE_REQUIRED"
    await tools.execute(
        "update_goal",
        json.dumps(
            {
                "changes": [
                    {
                        "field": "category",
                        "value": "炒菜",
                        "strength": "required",
                        "matching": "literal",
                    },
                ]
            }
        ),
    )
    assert not tools.goal_update_pending and tools.state.requirements.category_matching == "literal"


def test_next_turn_can_narrow_saved_mapping_but_cannot_widen_without_catalog():
    tools = tools_for()
    observe(tools)
    assert tools.apply_category_matches(["天津菜", "家常菜"]) is None
    next_turn = GroupBuyTools(None, tools.state, AsyncMock(), user_message="天津菜可以")
    assert next_turn.catalog_observation is None
    assert next_turn.apply_category_matches(["天津菜"]) is None
    assert next_turn.state.requirements.category == "炒菜"
    assert next_turn.state.requirements.category_matches == ["天津菜"]
    assert next_turn.state.category_mapping.source == "narrowed_mapping"
    assert next_turn.apply_category_matches(["天津菜", "家常菜"])["error"] == "CATALOG_REQUIRED"


@pytest.mark.mysql
async def test_real_sql_colloquial_candidates_and_known_scope_do_not_bypass_constraints(
    migration_database,
    mysql_settings,
):
    db = migration_database
    await provision(mysql_settings.model_copy(update={"mysql_database": db.engine.url.database}))
    await seed_catalog(db, load_catalog(), apply=True)
    await seed_rules(db, load_entries(), apply=True)
    state = ConversationState(
        requirements=Requirements(category="炒菜", category_matching="semantic")
    )
    tools = GroupBuyTools(db, state, AsyncMock(), user_message="想吃炒菜，有没有饭店优惠券推荐")
    catalog = await tools.execute("discover_options", "{}")
    assert catalog["literal_catalog_match"] is False
    result = await tools.execute(
        "search_group_buys",
        json.dumps(
            {
                "category_matches": ["天津菜", "家常菜"],
                "keywords": ["炒菜"],
            }
        ),
    )
    assert result["total"] == 2
    assert {row["category"] for row in result["items"]} == {"天津菜", "家常菜"}
    assert state.requirements.category == "炒菜"
    assert all(row["verdict"] == "unknown" for row in result["assessments"])
    # 下一轮复用的是保存的解释，不要求再发现一次目录；最终展示仍查当前事实。
    restored = ConversationState.model_validate(state.model_dump(mode="json"))
    next_tools = GroupBuyTools(db, restored, AsyncMock(), user_message="先看第一张")
    searched = await next_tools.execute("search_group_buys", "{}")
    vid = searched["items"][0]["voucher_id"]
    final = await next_tools.execute(
        "finish", json.dumps({"answer": "相关候选，菜品仍待核验", "display_ids": [vid]})
    )
    assert final["finished"] and restored.cards[0].verdict == "unknown"
    assert any(i.code == "CATEGORY_RELATED_UNVERIFIED" for i in restored.cards[0].issues)
    # 火锅在预算50元内无结果，不得因为当前预算目录未返回火锅就将其换成面馆。
    limited = GroupBuyTools(
        db,
        ConversationState(
            requirements=Requirements(
                category="火锅",
                category_matching="semantic",
                budget_fen=5000,
            )
        ),
        AsyncMock(),
    )
    discovered = await limited.execute("discover_options", "{}")
    assert discovered["literal_catalog_match"] is True
    available = discovered["options"][0]["category"]
    denied = await limited.execute(
        "search_group_buys", json.dumps({"category_matches": [available]})
    )
    assert denied["error"] == "LITERAL_CATEGORY_EXISTS"
    original = await limited.execute("search_group_buys", '{"keywords":["火锅"]}')
    assert original["total"] == 0 and limited.state.requirements.budget_fen == 5000
    assert original["feedback"]["counts_if_omitted"]["category"] > 0
    assert "keywords" not in original["feedback"]["counts_if_omitted"]
    async with db.sessions() as session:
        wildcard = await group_buys.search(
            session,
            SearchInput(
                requirements=Requirements(
                    category="炒菜",
                    category_matching="semantic",
                    category_matches=["%"],
                )
            ),
        )
    assert wildcard.total == 0
    # “分类名必须是炒菜”不能被标题中的炒菜字样绕过；旧文本匹配语义仍兼容。
    async with db.sessions() as session, session.begin():
        await session.execute(
            sql_update(Voucher).where(Voucher.id == 2012).values(title="炒菜字样的测试代金券")
        )
    async with db.sessions() as session:
        strict = await group_buys.search(
            session,
            SearchInput(
                requirements=Requirements(
                    category="炒菜",
                    category_matching="literal",
                )
            ),
        )
        legacy = await group_buys.search(
            session, SearchInput(requirements=Requirements(category="炒菜"))
        )
    assert strict.total == 0 and legacy.total == 1
    evaluated = group_buys.evaluate(
        legacy.items[0], Requirements(category="炒菜", category_matching="literal")
    )
    assert evaluated.verdict == "ineligible"
