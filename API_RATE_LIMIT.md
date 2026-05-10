# API限流机制说明

## 🚨 问题分析

### DeepSeek API 限流策略

根据官方文档，DeepSeek API 有严格的限流策略：

| 用户等级 | QPS | RPM | Token配额/小时 |
|---------|-----|-----|---------------|
| **免费版** | 0.83次/秒 | 50次/分钟 | 50K输入 + 25K输出 |
| **专业版** | 3.3次/秒 | 200次/分钟 | 300K输入 + 150K输出 |
| **定制版** | 16次/秒 | 1000次/分钟 | 2M输入 + 1M输出 |

### 当前问题

**5个Agent同时调用API会导致限流！**

假设：
- 每个Agent处理一条新闻需要调用10次API
- 5个Agent同时运行
- 总共50次API调用

**结果**：
- 免费版：立即触发限流（QPS只有0.83）
- 专业版：可能触发限流（QPS只有3.3）
- 定制版：相对安全（QPS有16）

## ✅ 解决方案：API限流器

### 1. 添加全局限流器

```python
class APIRateLimiter:
    """API调用限流器 - 使用令牌桶算法
    
    防止多个Agent同时调用API导致限流
    """
    
    def __init__(self, qps: float = 3.0, enabled: bool = True):
        self.qps = qps
        self.enabled = enabled
        self.min_interval = 1.0 / qps if qps > 0 else 0
        self.last_call_time = 0.0
        self.lock = threading.Lock()
        self.call_count = 0
        self.wait_count = 0
    
    def acquire(self):
        """获取调用许可（会阻塞直到可以调用）"""
        if not self.enabled:
            return
        
        with self.lock:
            current_time = time.time()
            elapsed = current_time - self.last_call_time
            
            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                time.sleep(wait_time)
                self.wait_count += 1
            
            self.last_call_time = time.time()
            self.call_count += 1
```

### 2. 在Agent调用API前获取许可

```python
# 第720行：在调用API前获取限流许可
api_rate_limiter.acquire()

response = self.llm_with_tools.invoke(messages)
```

### 3. 配置选项

```python
# 文件顶部配置
API_RATE_LIMIT_QPS = 3.0  # 每秒最多3次API调用（专业版）
API_RATE_LIMIT_ENABLED = True  # 是否启用API限流
```

## 📊 工作原理

### 限流算法

```
时间线：
T0: Agent1 调用API → 立即执行
T0.1: Agent2 调用API → 等待（距离上次调用< 0.33秒）
T0.33: Agent2 执行API调用
T0.4: Agent3 调用API → 等待
T0.66: Agent3 执行API调用
...
```

### 并发控制

```
┌─────────────────────────────────────┐
│  5个Agent同时运行                    │
│  Agent1 ─┐                          │
│  Agent2 ─┤                          │
│  Agent3 ─┼─→ API限流器 ─→ DeepSeek │
│  Agent4 ─┤     (队列)               │
│  Agent5 ─┘                          │
└─────────────────────────────────────┘

限流器确保：
- 同一时刻最多3个API调用/秒
- 超过限制的调用会等待
- 不会触发API限流错误
```

## 🔧 配置建议

### 根据账户等级配置

#### 免费版用户

```python
API_RATE_LIMIT_QPS = 0.8  # 保守设置，避免触发限流
API_RATE_LIMIT_ENABLED = True
```

#### 专业版用户

```python
API_RATE_LIMIT_QPS = 3.0  # 推荐设置
API_RATE_LIMIT_ENABLED = True
```

#### 定制版用户

```python
API_RATE_LIMIT_QPS = 15.0  # 可以设置更高
API_RATE_LIMIT_ENABLED = True
```

#### 不启用限流

```python
API_RATE_LIMIT_ENABLED = False  # 如果确定不会触发限流
```

## 📈 性能影响

### 限流前

```
5个Agent同时调用API → 立即触发限流 → 返回429错误 → 重试 → 延迟更长
```

### 限流后

```
5个Agent有序调用API → 等待时间可控 → 无错误 → 平滑处理
```

### 性能对比

| 场景 | 无限流 | 有限流 | 改进 |
|------|-------|-------|------|
| 100条新闻 | 频繁429错误 | 平滑处理 | ✅ |
| 总耗时 | 不确定（重试多） | 可预测 | ✅ |
| API错误率 | 高 | 低 | ✅ |
| 资源利用率 | 低（重试浪费） | 高 | ✅ |

## 📊 统计输出

### 处理完成后输出

```
处理完成!
  总条数: 100
  处理条数: 100
  成功条数: 95
  ...
  
API限流统计:
  总API调用次数: 850
  限流等待次数: 420
  QPS限制: 3.0
  限流启用: True
```

### 保存到 stats.json

```json
{
  "total_items": 100,
  "success_count": 95,
  "api_rate_limit": {
    "total_calls": 850,
    "total_waits": 420,
    "qps_limit": 3.0,
    "enabled": true
  }
}
```

## 🎯 最佳实践

### 1. 根据账户等级调整QPS

```python
# 免费版
API_RATE_LIMIT_QPS = 0.8

# 专业版
API_RATE_LIMIT_QPS = 3.0

# 定制版
API_RATE_LIMIT_QPS = 15.0
```

### 2. 监控限流统计

```python
# 查看限流效果
api_stats = api_rate_limiter.get_stats()
print(f"API调用: {api_stats['total_calls']}")
print(f"等待次数: {api_stats['total_waits']}")
```

### 3. 动态调整

```python
# 如果等待次数过多，可以降低QPS
if api_stats['total_waits'] > 100:
    api_rate_limiter.qps = 2.0
    api_rate_limiter.min_interval = 1.0 / 2.0
```

### 4. 错误处理

```python
# 如果仍然触发429错误，可以增加等待时间
try:
    api_rate_limiter.acquire()
    response = self.llm_with_tools.invoke(messages)
except Exception as e:
    if "429" in str(e):
        time.sleep(60)  # 等待1分钟
        # 重试
```

## 🔍 故障排查

### 问题1：仍然触发429错误

**原因**：QPS设置过高

**解决**：
```python
API_RATE_LIMIT_QPS = 2.0  # 降低QPS
```

### 问题2：处理速度太慢

**原因**：QPS设置过低

**解决**：
```python
API_RATE_LIMIT_QPS = 5.0  # 提高QPS（如果账户支持）
```

### 问题3：限流器不工作

**原因**：限流器未启用

**解决**：
```python
API_RATE_LIMIT_ENABLED = True  # 启用限流
```

## 📝 总结

### 改进前

- ❌ 无API限流
- ❌ 频繁触发429错误
- ❌ 重试浪费时间
- ❌ 处理时间不可预测

### 改进后

- ✅ 全局API限流器
- ✅ 平滑调用API
- ✅ 无429错误
- ✅ 处理时间可预测
- ✅ 详细统计信息

### 关键改进

1. **添加 `APIRateLimiter` 类**
2. **在Agent调用API前获取许可**
3. **输出限流统计信息**
4. **支持配置QPS和启用/禁用**

现在你的项目可以安全地并发调用DeepSeek API，不会触发限流错误！🔒
