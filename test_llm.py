from agents.knowledge_agent import KnowledgeAgent
from utils.logger import logger
import sys

def test_llm():
    logger.info("开始测试 LLM 连接...")
    
    # 1. 初始化
    agent = KnowledgeAgent()
    
    if not agent.has_llm:
        logger.error("LLM 初始化失败，请检查配置。")
        return

    # 2. 测试简单对话
    logger.info("测试简单对话...")
    try:
        from langchain_core.messages import HumanMessage
        response = agent.llm.invoke([HumanMessage(content="你好，请介绍一下你自己，20字以内。")])
        logger.info(f"LLM 回复: {response.content}")
    except Exception as e:
        logger.error(f"对话测试失败: {e}")
        return

    # 3. 测试 Embedding (如果有)
    if agent.embeddings:
        logger.info("测试 Embedding...")
        try:
            vec = agent.embeddings.embed_query("测试文本")
            logger.info(f"Embedding 成功，向量维度: {len(vec)}")
        except Exception as e:
            logger.error(f"Embedding 测试失败: {e}")
    else:
        logger.warning("Embedding 未初始化，RAG 功能将不可用。")

if __name__ == "__main__":
    test_llm()
