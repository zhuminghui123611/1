import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, Callable, TypeVar, Union
import httpx
from urllib.parse import urljoin
import json
from functools import wraps

from app.core.config import settings
from app.core.exceptions import ExternalAPIException, ServiceUnavailableException
from app.db.redis import RedisClient
from app.models.market_data import DataSourceType

logger = logging.getLogger(__name__)

# 定义类型变量用于泛型函数
T = TypeVar('T')

class APIRateLimiter:
    """API速率限制器类"""
    
    def __init__(self, calls_limit: int, time_period: int):
        """
        初始化速率限制器
        
        Args:
            calls_limit: 时间周期内允许的调用次数
            time_period: 时间周期(秒)
        """
        self.calls_limit = calls_limit
        self.time_period = time_period
        self.calls_timestamps = []
    
    async def wait_if_needed(self):
        """
        如果达到速率限制则等待
        """
        now = time.time()
        
        # 移除过期的时间戳
        self.calls_timestamps = [ts for ts in self.calls_timestamps if now - ts < self.time_period]
        
        # 如果达到限制则等待
        if len(self.calls_timestamps) >= self.calls_limit:
            oldest_timestamp = self.calls_timestamps[0]
            wait_time = self.time_period - (now - oldest_timestamp)
            
            if wait_time > 0:
                logger.info(f"达到API速率限制，等待 {wait_time:.2f} 秒")
                await asyncio.sleep(wait_time)
                # 递归检查是否仍需等待
                await self.wait_if_needed()
        
        # 添加当前调用的时间戳
        self.calls_timestamps.append(time.time())


def with_retry(max_retries: int = 3, retry_delay: float = 1.0, backoff_factor: float = 2.0):
    """
    重试装饰器
    
    Args:
        max_retries: 最大重试次数
        retry_delay: 初始重试延迟(秒)
        backoff_factor: 退避因子
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            delay = retry_delay
            
            for retry_count in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except ExternalAPIException as e:
                    last_exception = e
                    
                    # 如果是不可恢复的错误，不再重试
                    if e.status_code in [401, 403, 404]:
                        logger.warning(f"不可恢复的API错误，不再重试: {str(e)}")
                        raise
                    
                    # 如果已达到最大重试次数，抛出异常
                    if retry_count >= max_retries:
                        logger.error(f"达到最大重试次数({max_retries})，放弃请求: {str(e)}")
                        raise
                    
                    logger.warning(f"API请求失败，将在 {delay:.2f} 秒后重试 ({retry_count + 1}/{max_retries}): {str(e)}")
                    await asyncio.sleep(delay)
                    delay *= backoff_factor
                    
            # 如果所有重试都失败，抛出最后一个异常
            if last_exception:
                raise last_exception
                
        return wrapper
    return decorator


def with_cache(ttl: int = 300, cache_key_prefix: str = "api_cache"):
    """
    缓存装饰器
    
    Args:
        ttl: 缓存生存时间(秒)
        cache_key_prefix: 缓存键前缀
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 生成缓存键
            key_parts = [cache_key_prefix, func.__name__]
            
            # 添加参数到缓存键
            for arg in args[1:]:  # 跳过self参数
                key_parts.append(str(arg))
            
            # 添加关键字参数
            for k, v in sorted(kwargs.items()):
                key_parts.append(f"{k}={v}")
            
            cache_key = ":".join(key_parts)
            
            # 尝试从缓存获取
            cached_data = RedisClient.get(cache_key)
            if cached_data:
                logger.debug(f"从缓存获取数据: {cache_key}")
                return json.loads(cached_data)
            
            # 获取新数据
            result = await func(*args, **kwargs)
            
            # 保存到缓存
            try:
                RedisClient.set(cache_key, json.dumps(result), ex=ttl)
                logger.debug(f"数据保存到缓存: {cache_key}, TTL={ttl}秒")
            except Exception as e:
                logger.warning(f"保存数据到缓存失败: {str(e)}")
            
            return result
        
        return wrapper
    return decorator


class DataIntegrationService:
    """数据集成服务，处理与外部API的交互"""
    
    # API速率限制器配置
    _rate_limiters = {
        DataSourceType.ANKR: APIRateLimiter(calls_limit=10, time_period=1),  # 10 calls per second
        DataSourceType.RESERVOIR: APIRateLimiter(calls_limit=60, time_period=60),  # 60 calls per minute
        DataSourceType.OKX_P2P: APIRateLimiter(calls_limit=5, time_period=1),  # 5 calls per second
        DataSourceType.ONEINCH: APIRateLimiter(calls_limit=100, time_period=60),  # 100 calls per minute
    }
    
    # API基础URL配置
    _base_urls = {
        DataSourceType.ANKR: "https://rpc.ankr.com/multichain/",
        DataSourceType.RESERVOIR: "https://api.reservoir.tools/",
        DataSourceType.OKX_P2P: "https://www.okx.com/api/v5/p2p/",
        DataSourceType.ONEINCH: "https://api.1inch.io/v5.0/",
    }
    
    # API请求头配置
    _api_headers = {
        DataSourceType.ANKR: {
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        DataSourceType.RESERVOIR: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-KEY": settings.RESERVOIR_API_KEY,
        },
        DataSourceType.OKX_P2P: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OK-ACCESS-KEY": settings.OKX_API_KEY,
            "OK-ACCESS-PASSPHRASE": settings.OKX_API_PASSPHRASE,
        },
        DataSourceType.ONEINCH: {
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    }
    
    @classmethod
    async def _make_api_request(
        cls,
        data_source: DataSourceType,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        发起API请求
        
        Args:
            data_source: 数据源类型
            method: HTTP方法 (GET, POST, etc.)
            endpoint: API端点
            params: 查询参数
            data: 请求体数据
            timeout: 超时时间(秒)
            headers: 额外的请求头
            
        Returns:
            Dict[str, Any]: API响应数据
            
        Raises:
            ExternalAPIException: 当API请求失败时
        """
        # 获取速率限制器
        rate_limiter = cls._rate_limiters.get(data_source)
        if rate_limiter:
            await rate_limiter.wait_if_needed()
        
        # 构建URL
        base_url = cls._base_urls.get(data_source)
        if not base_url:
            raise ExternalAPIException(f"未知数据源: {data_source}")
        
        url = urljoin(base_url, endpoint)
        
        # 合并请求头
        request_headers = cls._api_headers.get(data_source, {}).copy()
        if headers:
            request_headers.update(headers)
        
        # 根据OKX API的要求生成签名 (如果需要)
        if data_source == DataSourceType.OKX_P2P and settings.OKX_API_SECRET:
            from app.utils.crypto import generate_okx_signature
            timestamp = str(int(time.time() * 1000))
            request_headers["OK-ACCESS-TIMESTAMP"] = timestamp
            
            # 生成签名
            if method == "GET":
                sign_str = timestamp + method + f"/{endpoint}"
                if params:
                    query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
                    sign_str += f"?{query_string}"
            else:
                sign_str = timestamp + method + f"/{endpoint}"
                if data:
                    sign_str += json.dumps(data)
            
            sign = generate_okx_signature(sign_str, settings.OKX_API_SECRET)
            request_headers["OK-ACCESS-SIGN"] = sign
        
        try:
            logger.debug(f"发起{data_source} API请求: {method} {url}")
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    response = await client.get(url, params=params, headers=request_headers)
                elif method.upper() == "POST":
                    response = await client.post(url, params=params, json=data, headers=request_headers)
                else:
                    raise ExternalAPIException(f"不支持的HTTP方法: {method}")
                
                # 检查HTTP状态码
                if response.status_code >= 400:
                    error_msg = f"{data_source} API请求失败 ({response.status_code}): {response.text}"
                    logger.error(error_msg)
                    raise ExternalAPIException(error_msg)
                
                # 解析JSON响应
                try:
                    return response.json()
                except json.JSONDecodeError:
                    error_msg = f"{data_source} API响应不是有效的JSON: {response.text}"
                    logger.error(error_msg)
                    raise ExternalAPIException(error_msg)
                
        except httpx.RequestError as e:
            error_msg = f"{data_source} API请求错误: {str(e)}"
            logger.error(error_msg)
            raise ExternalAPIException(error_msg)
        except Exception as e:
            error_msg = f"{data_source} API请求异常: {str(e)}"
            logger.error(error_msg)
            raise ExternalAPIException(error_msg)
    
    @classmethod
    @with_retry(max_retries=3, retry_delay=1.0, backoff_factor=2.0)
    @with_cache(ttl=300, cache_key_prefix="ankr_api")
    async def fetch_ankr_data(cls, chain: str, method: str, params: List[Any]) -> Dict[str, Any]:
        """
        从Ankr API获取区块链数据
        
        Args:
            chain: 区块链名称
            method: JSON-RPC方法
            params: 方法参数
            
        Returns:
            Dict[str, Any]: Ankr API响应数据
        """
        request_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        if settings.ANKR_API_KEY:
            headers["ankr-api-key"] = settings.ANKR_API_KEY
        
        response = await cls._make_api_request(
            data_source=DataSourceType.ANKR,
            method="POST",
            endpoint=chain,
            data=request_data,
            headers=headers
        )
        
        if "error" in response:
            error_msg = f"Ankr API错误: {response['error']}"
            logger.error(error_msg)
            raise ExternalAPIException(error_msg)
        
        return response["result"]
    
    @classmethod
    @with_retry(max_retries=3, retry_delay=1.0, backoff_factor=2.0)
    @with_cache(ttl=300, cache_key_prefix="reservoir_api")
    async def fetch_reservoir_data(cls, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        从Reservoir API获取NFT数据
        
        Args:
            endpoint: API端点
            params: 查询参数
            
        Returns:
            Dict[str, Any]: Reservoir API响应数据
        """
        response = await cls._make_api_request(
            data_source=DataSourceType.RESERVOIR,
            method="GET",
            endpoint=endpoint,
            params=params,
        )
        
        return response
    
    @classmethod
    @with_retry(max_retries=3, retry_delay=1.0, backoff_factor=2.0)
    @with_cache(ttl=60, cache_key_prefix="okx_p2p_api")
    async def fetch_okx_p2p_data(
        cls, 
        endpoint: str, 
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        method: str = "GET"
    ) -> Dict[str, Any]:
        """
        从OKX P2P API获取数据
        
        Args:
            endpoint: API端点
            params: 查询参数
            data: 请求体数据
            method: HTTP方法
            
        Returns:
            Dict[str, Any]: OKX P2P API响应数据
        """
        response = await cls._make_api_request(
            data_source=DataSourceType.OKX_P2P,
            method=method,
            endpoint=endpoint,
            params=params,
            data=data,
        )
        
        # OKX API通常包含一个code字段，0表示成功
        if response.get("code") != "0":
            error_msg = f"OKX P2P API错误: {response.get('msg', 'Unknown error')}"
            logger.error(error_msg)
            raise ExternalAPIException(error_msg)
        
        return response["data"]
    
    @classmethod
    @with_retry(max_retries=3, retry_delay=1.0, backoff_factor=2.0)
    @with_cache(ttl=60, cache_key_prefix="oneinch_api")
    async def fetch_oneinch_data(cls, chain_id: int, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        从1inch API获取数据
        
        Args:
            chain_id: 区块链ID
            endpoint: API端点
            params: 查询参数
            
        Returns:
            Dict[str, Any]: 1inch API响应数据
        """
        full_endpoint = f"{chain_id}/{endpoint}"
        
        response = await cls._make_api_request(
            data_source=DataSourceType.ONEINCH,
            method="GET",
            endpoint=full_endpoint,
            params=params,
        )
        
        return response
    
    @classmethod
    async def handle_data_source_exception(
        cls, 
        source: DataSourceType, 
        func: Callable[..., T], 
        fallback_value: Optional[T] = None,
        log_error: bool = True,
        *args, 
        **kwargs
    ) -> Union[T, None]:
        """
        处理数据源异常，实现优雅降级
        
        Args:
            source: 数据源类型
            func: 要调用的函数
            fallback_value: 发生异常时的返回值
            log_error: 是否记录错误
            *args: 函数参数
            **kwargs: 函数关键字参数
            
        Returns:
            成功时返回函数结果，失败时返回fallback_value
        """
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if log_error:
                logger.error(f"{source} 数据源异常: {str(e)}")
            return fallback_value 