
from db.lance_db_service import LanceDBServer
from umail_lance.umail_lance_agent import UmailLanceClient

class Bytoid_pro_lance:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.lance_service = LanceDBServer()
        self.emb_client = UmailLanceClient(user_id)


    
    async def insert_to_lance(self, chat):

        texts = [c.content for c in chat if c.content]

        # batch embedding (more efficient)
        vectors = await self.emb_client.safe_embed_chunks(texts, user_id=self.user_id)

        vec_idx = 0
        for c in chat:
            if c.content:
                c.embedding = vectors[vec_idx]
                vec_idx += 1

        return await self.lance_service.insert_chat(chat, self.user_id)
    

    def get_history(self, last_timestamp):
        lance_response = self.lance_service.get_user_chats_by_timestamp( user_id = self.user_id, last_timestamp = last_timestamp)
        return lance_response
    
    def get_chat(self, chat_id):
        lance_response = self.lance_service.get_chat_by_id(self.user_id, chat_id)
        return lance_response

    
    # async def embed_chat_message(self, text: str) -> list[float]:
    #     """
    #     Embed a single chat message safely using existing infra.
    #     """
    #     emb_obj = UmailLanceClient(self.user_id)
    #     vectors = await emb_obj.safe_embed_chunks([text], user_id=self.user_id)

    #     return vectors[0] if vectors else []
    
    async def get_context(self, message, chat_id):
        print("inside get context")
        vector = await self.emb_client.safe_embed_chunks(message, user_id=self.user_id)
    
        context = self.lance_service.find_semantic_matches(vector, self.user_id, chat_id)
        return context



    def delete_table(self):
        lance_response = self.lance_service.table_delete(self.user_id)
        return lance_response