class DummyDashboardWSManager:
    async def connect(self, websocket, client_id=None):
        await websocket.accept()

    async def disconnect(self, websocket, client_id=None):
        return None

    def client_count(self):
        return 0

    async def send_personal_message(self, message, websocket):
        if isinstance(message, (dict, list)):
            await websocket.send_json(message)
        else:
            await websocket.send_text(str(message))

    async def broadcast(self, message):
        return None


dashboard_ws_manager = DummyDashboardWSManager()
