import asyncio
import websockets
import logging
import uuid
from asyncio import Queue
from WSUtils import WSUtils
# pip install requests
# 设置日志记录
logging.basicConfig(level=logging.INFO,format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

class WebSocketGateway:
    
    def __init__(self):
        self.clients = {}  # 存储当前连接的客户端，键为客户端标识符，值为 WebSocket 连接
        self.backend_ws = None  # 与 BackendServer 的连接
        self.backend_queue = Queue()
        self.client_queue = Queue()
        self.backend_task = asyncio.create_task(self.handle_backend_queue())  # 后台任务，用于向 BackendServer 发送消息
        self.client_task = asyncio.create_task(self.handle_client_queue())  # 客户端任务，用于向客户端发送消息

    async def handle_backend_queue(self):
        """ 向 BackendServer 发送消息 """
        while True:
            try:
                logger.debug(f"开始从队列backend_queue中获取消息")
                message = await self.backend_queue.get()  # 从队列中获取消息
                if message =="BackendServerClosed":
                    self.backend_queue= Queue()
                    continue
                if self.backend_ws is not None:
                    await self.backend_ws.send(message)           
                    logger.debug(f"消息已发送到后端服务: {message}")
            except Exception:
                logger.exception("向 BackendServer 发送消息失败")
            finally:
                try:
                    self.backend_queue.task_done()
                except Exception:
                    logger.exception("从队列backend_queue中删除消息失败")
                
                await asyncio.sleep(0.01)

    async def handle_client_queue(self):
        """ 向 客户端 发送消息 """
        while True:
            try:
                message = await self.client_queue.get()  # 从队列中获取消息
                client_id, message_type, message  = WSUtils.parse_message(message)
              
                if self.clients.get(client_id) is not None:
                      if message_type == "backend_local_disconnect":
                        try:     
                            await self.clients.get(client_id).close()                                                  
                        except Exception:
                            logger.exception(f"{client_id} 关闭客户端连接失败")
                        del self.clients[client_id]  # 清理断开的客户端连接
                      else:
                        await self.clients.get(client_id).send(message)                   
                        logger.debug(f"消息已发送到客户端: {message}")
            except Exception:
                logger.exception("向 客户端 发送消息失败")
            finally:
                try:
                    self.client_queue.task_done()
                except Exception:
                    logger.exception("从队列client_queue中删除消息失败")
                await asyncio.sleep(0.01)

        
    async def handle_client(self, websocket, path):
        """ 处理来自 WebSocket 客户端的连接 """
        client_id = str(uuid.uuid4())  # 生成唯一的客户端标识符
        self.clients[client_id] = websocket  # 存储客户端连接
        logger.info(f"已连接到客户端 {client_id}")
        
        try:
            async for message in websocket:
                #logger.debug(f"从客户端 {client_id} 收到消息: {message}")
                w_message = WSUtils.add_message(message,client_id)
                if self.backend_ws is not None:
                    await self.backend_queue.put(w_message)
                    logger.debug(f"消息已放入队列: {w_message}")
                else:
                    raise Exception("BackendServer 尚未连接，不能连接")
        except Exception as e:
            logger.exception(f"客户端 {client_id} 连接关闭: {e}")
        finally:
            w_message = WSUtils.add_message(b"client_disconnect",client_id)
            try:
                if self.backend_ws is not None:
                    await self.backend_queue.put(w_message)
                    await self.clients[client_id].close()
                    del self.clients[client_id]  # 清理断开的客户端连接
            finally:
                pass
           
            logger.info(f"客户端 {client_id} 已断开连接")

    async def handle_backend_connection(self, websocket, path):
        """ 处理来自 BackendServer 的连接 """
        self.backend_ws = websocket  # 保存与 BackendServer 的连接
        logger.info("已连接到 BackendServer")
        try:
            async for message in websocket:
                # 从服务器接收消息并放入队列
                await self.client_queue.put(message)  
        except (websockets.exceptions.ConnectionClosedError, 
                websockets.exceptions.ConnectionClosedOK) as e:
            logger.exception("BackendServer 连接断开")
            self.backend_ws = None  # 清理断开的 BackendServer 连接
            await self.backend_queue.put("BackendServerClosed")  # 停止向 BackendServer 发送消息
            for client_id,client_ws in self.clients.copy().items():
                try:
                    await client_ws.close()
                finally:
                    pass
                del self.clients[client_id] 
        finally:
            pass

import os
from aiohttp import web, ClientSession
import ssl

class HttpProxy():
    
    def __init__(self):
        self.ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        
    def getProxies(self,scheme):
        if scheme == 'https':
            proxies =os.environ.get("https_proxy")# 从环境变量获取 HTTPS 代理地址
        elif scheme == 'http':
            proxies =os.environ.get("http_proxy")# 从环境变量获取 HTTP 代理地址
        return proxies
    
    async def handle_request(self,request):
        # 获取原始请求的 URL
        requstHost='generativelanguage.googleapis.com'
        if 'models/gemini' in request.path:
            requstHost='generativelanguage.googleapis.com'

        original_url = request.scheme + '://' + requstHost + request.path
        
        # 根据请求方法进行处理
        async with ClientSession() as session:
            if request.method == 'POST':
                post_data = await request.post()
                ssl_context = self.ssl_context
                if request.scheme == 'http':
                    ssl_context=None
                headers = {key: value for key, value in request.headers.items()}  # 获取原始请求头
                # 转发 POST 请求
                async with session.post(original_url, headers=headers, data=post_data, proxy=self.getProxies(request.scheme), ssl=ssl_context) as response:
                    logger.info(f"转发请求 {original_url}")
                    # 将响应返回给客户端
                    response=web.Response(
                        status=response.status,
                        body=await response.read(),
                        headers=response.headers
                    )
                    logger.info(f"响应 {response}")
                    return response
            else:  # 默认为 GET 请求
                # 转发 GET 请求
                async with session.get(original_url, headers=headers, proxy=self.getProxies(request.scheme), ssl=ssl_context) as response:
                    logger.info(f"转发请求 {original_url}")
                    # 将响应返回给客户端
                    response=web.Response(
                        status=response.status,
                        body=await response.read(),
                        headers=response.headers
                    )
                    logger.info(f"响应 {response}")
                    return response
  
        

async def main():
    gateway = WebSocketGateway()

    # 启动 WebSocket 服务器监听客户端连接
    client_server = await websockets.serve(gateway.handle_client, "0.0.0.0", 10095)
    logger.info("WebSocket 客户端服务已启动，在 10095 端口监听...")

    # 启动 WebSocket 服务器监听 BackendServer 的连接
    backend_server = await websockets.serve(gateway.handle_backend_connection, "0.0.0.0", 10096)
    logger.info("WebSocket 后端服务已启动，在 10096 端口监听...")
    
     # 启动 aiohttp HTTP 服务器
    app = web.Application()
    handle = HttpProxy()
    app.router.add_route('*', '/{tail:.*}', handle.handle_request)  # 捕获所有的 GET 和 POST 请求
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain('/etc/letsencrypt/live/whxyxlzx.com.cn/fullchain.pem', '/etc/letsencrypt/live/whxyxlzx.com.cn/privkey.pem')
    site = web.TCPSite(runner, '0.0.0.0', 10099, ssl_context=ssl_context)
    await site.start()

    logger.info(f"监听端口10099...")

    await asyncio.Event().wait()  # 持续运行，直到外部终止

if __name__ == "__main__":
    asyncio.run(main())
