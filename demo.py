"""
LangChain MongoDB Agent with Custom OpenAI Endpoint
"""

from typing import List, Dict, Any, Optional
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from langchain.agents import AgentType, initialize_agent
from langchain.tools import Tool
from langchain.chat_models import ChatOpenAI
from langchain.schema import SystemMessage
from langchain.prompts import MessagesPlaceholder
from langchain.memory import ConversationBufferMemory
from openai import OpenAI

class MongoDBManager:
    """MongoDB connection manager with error handling"""
    
    def __init__(self, mongo_uri: str, db_name: str):
        self.client = MongoClient(mongo_uri)
        self.db = self.client[db_name]
        self._create_sample_data()
    
    def _create_sample_data(self):
        """Initialize sample product collection"""
        if "products" not in self.db.list_collection_names():
            self.db.products.insert_many([
                {"name": "Laptop", "price": 999, "stock": 15, "category": "electronics"},
                {"name": "Phone", "price": 699, "stock": 30, "category": "electronics"},
                {"name": "Tablet", "price": 299, "stock": 0, "category": "electronics"}
            ])
    
    @staticmethod
    def handle_errors(func):
        """Decorator for MongoDB error handling"""
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except PyMongoError as e:
                return f"MongoDB Operation Failed: {str(e)}"
        return wrapper

class MongoOperations:
    """Core MongoDB operations for LangChain tools"""
    
    def __init__(self, mongo_uri: str, db_name: str):
        self.manager = MongoDBManager(mongo_uri, db_name)
        self.tools = self._build_tools()
    
    def _build_tools(self) -> List[Tool]:
        """Create LangChain tools"""
        return [
            Tool(
                name="mongo_find",
                func=self.find_documents,
                description="Query documents from collection. Input format: "
                          "{'collection': 'name', 'filter': {}, 'projection': {}, 'limit': 5}"
            ),
            Tool(
                name="mongo_insert",
                func=self.insert_document,
                description="Insert document into collection. Input format: "
                          "{'collection': 'name', 'document': {}}"
            ),
            Tool(
                name="mongo_update",
                func=self.update_documents,
                description="Update documents in collection. Input format: "
                          "{'collection': 'name', 'filter': {}, 'update': {}}"
            )
        ]
    def close_connection(self):
        """关闭 MongoDB 连接"""
        self.manager.client.close()
    
    @MongoDBManager.handle_errors
    def find_documents(self, query: Dict) -> List[Dict]:
        """Find documents with projection"""
        collection = self.manager.db[query["collection"]]
        return list(collection.find(
            filter=query.get("filter", {}),
            projection=query.get("projection", {"_id": 0}),
            limit=query.get("limit", 5)
        ))
    
    @MongoDBManager.handle_errors
    def insert_document(self, data: Dict) -> str:
        """Insert single document"""
        collection = self.manager.db[data["collection"]]
        result = collection.insert_one(data["document"])
        return f"Inserted ID: {result.inserted_id}"
    
    @MongoDBManager.handle_errors
    def update_documents(self, data: Dict) -> str:
        """Update multiple documents"""
        collection = self.manager.db[data["collection"]]
        result = collection.update_many(
            filter=data["filter"],
            update=data["update"]
        )
        return f"Modified {result.modified_count} documents"

def create_custom_agent(mongo_uri: str, db_name: str) -> Any:
    """Create LangChain agent with custom OpenAI config"""
    # 配置自定义OpenAI客户端
    custom_client = OpenAI(
        api_key="sk-kkghgpuolccfnrgzvszfzrgzridilivxarjaejunjlrqnsrc",
        base_url="https://api.siliconflow.cn/v1"
    )
    
    # 创建LangChain LLM实例
    llm = ChatOpenAI(
        openai_api_key="sk-kkghgpuolccfnrgzvszfzrgzridilivxarjaejunjlrqnsrc",
        openai_api_base="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-32B-Instruct",
        temperature=0
    )
    
    # 初始化MongoDB工具
    mongo_ops = MongoOperations(mongo_uri, db_name)
    
    # 配置Agent
    agent_kwargs = {
        "system_message": SystemMessage(content="""
            You are a MongoDB expert assistant. Follow these rules:
            1. Always convert natural language to proper MongoDB operations
            2. Use ISO date formats for datetime queries
            3. Limit results to 5 items unless specified
            4. Never modify the _id field
            5. Use projection to exclude _id by default
            """),
        "extra_prompt_messages": [MessagesPlaceholder(variable_name="memory")]
    }
    
    memory = ConversationBufferMemory(
        memory_key="memory",
        return_messages=True,
        output_key="output"
    )
    
    return initialize_agent(
        tools=mongo_ops.tools,
        llm=llm,
        agent=AgentType.OPENAI_FUNCTIONS,
        agent_kwargs=agent_kwargs,
        memory=memory,
        verbose=True,
        handle_parsing_errors=True
    )

if __name__ == "__main__":
    MONGO_URI = "mongodb://localhost:27017"
    DB_NAME = "ecommerce"
    
    # 创建 MongoOperations 实例
    mongo_ops = MongoOperations(MONGO_URI, DB_NAME)
    
    # 创建 Agent
    agent = create_custom_agent(MONGO_URI, DB_NAME)
    
    # 示例对话
    queries = [
        "显示所有价格低于800美元的电子产品",
        "插入一个新的书籍商品，价格19.99美元，库存100",
        "更新所有库存为0的商品状态为缺货",
        "查找库存大于20的电子产品"
    ]
    
    for query in queries:
        print(f"\n用户问题: {query}")
        response = agent.run(query)
        print(f"Agent响应: {response}")
    
    # 正确关闭连接
    mongo_ops.close_connection()  # 新增的清理方法
