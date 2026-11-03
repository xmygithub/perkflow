"""仅保存完整对话轮次；用版本冲突避免两次并发回答互相覆盖。"""

import asyncio
from uuid import uuid4

from sqlalchemy import func, select, update

from agent.schemas import ConversationOut, ConversationState
from backend.app.core.errors import ApiError
from backend.app.models import AgentConversation


async def create(database, user_id: int, timeout: int = 10) -> ConversationOut:
    # 1. 用户取认证依赖；限制会话数量，避免无上限创建学习数据。
    async with asyncio.timeout(timeout), database.sessions() as session, session.begin():
        count = await session.scalar(
            select(func.count())
            .select_from(AgentConversation)
            .where(AgentConversation.user_id == user_id)
        )
        if count >= 100:
            raise ApiError(429, "AGENT_CONVERSATION_LIMIT", "会话已达100个，请先整理历史数据")
        identifier, state = str(uuid4()), ConversationState()
        session.add(
            AgentConversation(
                id=identifier, user_id=user_id, version=0, state=state.model_dump(mode="json")
            )
        )
    return ConversationOut(id=identifier, version=0, state=state)


async def get(database, user_id: int, identifier: str, timeout: int = 10) -> ConversationOut:
    async with asyncio.timeout(timeout), database.sessions() as session:
        row = await session.scalar(
            select(AgentConversation).where(
                AgentConversation.id == identifier,
                AgentConversation.user_id == user_id,
            )
        )
        if row is None:
            raise ApiError(404, "AGENT_CONVERSATION_NOT_FOUND", "会话不存在")
        return ConversationOut(
            id=row.id, version=row.version, state=ConversationState.model_validate(row.state)
        )


async def save(
    database, user_id: int, conversation: ConversationOut, timeout: int = 10
) -> ConversationOut:
    # 2. 只在完整一轮成功后提交；不跨模型调用保持事务。失败、断流没有部分条件落库。
    async with asyncio.timeout(timeout), database.sessions() as session, session.begin():
        result = await session.execute(
            update(AgentConversation)
            .where(
                AgentConversation.id == conversation.id,
                AgentConversation.user_id == user_id,
                AgentConversation.version == conversation.version,
            )
            .values(
                state=conversation.state.model_dump(mode="json"),
                version=AgentConversation.version + 1,
            )
        )
        if result.rowcount != 1:
            raise ApiError(409, "AGENT_VERSION_CONFLICT", "会话已被另一请求更新，请刷新后继续")
    return conversation.model_copy(update={"version": conversation.version + 1})
