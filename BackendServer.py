import asyncio
import websockets
import logging
import json
from asyncio import Queue
import struct
import os
from WSUtils import WSUtils

# 设置日志记录
logging.basicConfig(level=logging.INFO,format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

class BackendServer:
    def __init__(self):
        self.gateway_ws = None  # 与 WebSocketGateway 的连接
        self.local_ws = {}     # 与本地语音服务器的连接
        self.localURL=os.environ.get("LOCAL_URI", "ws://localhost:10095")  # 本地语音服务器的 URL
        self.gatewayURL=os.environ.get("GATEWAY_URI", "ws://whxyxlzx.com.cn:10096")  # WebSocketGateway 的 URL
        self.clientTaskMap={}
        #asyncio.create_task(self.recv_local()) 

    async def connect_to_gateway(self):
        """ 连接到 WebSocketGateway，包含重试机制 """
        while self.gateway_ws is None:
            try:
                self.gateway_ws = await websockets.connect(self.gatewayURL)
                logger.info("已连接到 WebSocketGateway")
                break  # 连接成功，退出循环

            except Exception as e:
                logger.exception(f"连接 WebSocketGateway 失败: {e}")
                await asyncio.sleep(1)  # 1 秒后重试


    async def ping_gateway(self):
        """ 向 WebSocketGateway 发送心跳包，以维持连接 """
        while True:
            try:
                logger.debug("发送心跳包")
                await self.gateway_ws.send("pingFromBackend")  # 发送心跳包
                logger.debug("发送心跳包完成")
                await asyncio.sleep(10)  # 10 秒发送一次心跳包
            except Exception as e:
                logger.exception(f"向 WebSocketGateway 发送心跳包失败: {e}")
                self.gateway_ws = None
                await self.connect_to_gateway()  # 重新连接 WebSocketGateway
                

    async def connect_to_local(self,client_id):
        """ 连接到本地语音服务器，包含重连机制 """
        return_local_ws=None
        
        if client_id not in self.local_ws.copy():  # 只在未连接时尝试连接
            try:
                return_local_ws = await websockets.connect(self.localURL)
            except Exception as e:
                logger.exception(f"{client_id}连接本地语音服务器失败,开始重试: {e}")
                await asyncio.sleep(1)  # 1 秒后重试
                del self.local_ws[client_id]

            self.local_ws[client_id]=return_local_ws
            logger.info(f"{client_id}已连接到本地语音服务器")
        else:
            return_local_ws = self.local_ws[client_id]
            if not return_local_ws.open:  # 若连接已断开，则重新连接
                logger.info(f"{client_id}到本地语音服务器的连接失效，重新连接")
                del self.local_ws[client_id]
                await self.connect_to_local(client_id)
                

            logger.debug(f"{client_id}已连接到本地语音服务器，使用已有连接")
        return return_local_ws
    
    async def close_local(self,client_id):
        """ 关闭与本地语音服务器的连接 """
        try:
            client_ws = self.local_ws.pop(client_id, None)
            if client_ws and client_ws.open:
                await client_ws.close()
                logger.info(f"已断开与 {client_id} 的连接")
            try:
                await self.gateway_ws.send(WSUtils.add_message(b"backend_local_disconnect",client_id))  # 通知 WebSocketGateway 客户端断开连接
            except Exception as e:
                logger.exception(f"通知 WebSocketGateway 客户端断开连接失败: {e}")
        except Exception as e:
            logger.exception(f"断开与 {client_id} 的连接失败: {e}")

    async def handle_gateway_messages(self):        
        """ 处理来自 WebSocketGateway 的消息，并转发到本地语音服务器 """
        logger.debug("启动处理来自 WebSocketGateway 的消息任务")
        while True:
            try:
                async for message in self.gateway_ws:
                    try:
                        # 提取客户端ID
                        client_id, message_type, message=WSUtils.parse_message(message)
                        logger.debug(f"解析消息: {client_id},{message_type},{len(message)}")
                        if message_type=="client_disconnect":       
                            await self.close_local(client_id)  # 关闭与本地语音服务器的连接
                            
                        elif message_type=="json_data" or message_type=="audio_data":
                            logger.debug(f"消息内容: {message}")
                            try:
                                client=await self.connect_to_local(client_id)  # 连接到本地语音服务器  
                                await client.send(message)  # 直接转发消息
                                logger.debug(f"发送消息到本地语音服务器完成: {client_id},{message_type},{len(message)}")
                           
                            except Exception as e:
                                await self.close_local(client_id)  # 关闭与本地语音服务器的连接
                                logger.exception(f"发送消息到本地语音服务器失败: {e}")
                        
                    except Exception as e:
                        logger.exception(f"处理消息失败: {e}")

                    
            except websockets.ConnectionClosedError as e:
                    self.gateway_ws = None
                    logger.exception(f"WebSocketGateway 连接断开: {e}")
                    await self.connect_to_gateway()  # 重新连接 WebSocketGateway
            await asyncio.sleep(0.1)  # 等待一段时间以避免忙等待

    async def recv_local(self):
        while True:
            for client_id, client in self.local_ws.copy().items():
                try:
                    async for message in client:
                        logger.debug(f"从本地语音服务器收到消息: {client_id},{len(message)}")
                        message = WSUtils.add_message(message,client_id)
                        if self.gateway_ws is not None:  # 若 WebSocketGateway 已连接，则转发消息
                            try:
                                await self.gateway_ws.send(message)  # 等待响应并转发给 WebSocketGateway
                            except Exception as e:
                                logger.exception(f"发送消息到 WebSocketGateway 失败: {e}")
                                await self.connect_to_gateway()  # 重新连接 WebSocketGateway
                            logger.debug(f"发送消息到 WebSocketGateway 完成: {client_id},{len(message)}")
                except Exception as e:
                    logger.exception(f"从本地语音服务器收到消息失败: {e}")
                    await self.close_local(client_id)  # 重新连接本地语音服务器
                   
            await asyncio.sleep(0.1)  # 等待一段时间以避免忙等待

async def main():
    backend_server = BackendServer()
    await backend_server.connect_to_gateway()  # 连接到 WebSocketGateway

    # 启动两个处理任务
    asyncio.create_task(backend_server.recv_local()) 
    asyncio.create_task(backend_server.handle_gateway_messages())  # 处理从 WebSocketGateway 的消息

    await asyncio.Event().wait()    # 持续运行，直到外部终止

if __name__ == "__main__":
    asyncio.run(main())
