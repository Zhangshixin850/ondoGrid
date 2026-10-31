首先这是一个运行在ubuntu上面的多平台中性网格python脚本，请仔细研究脚本的逻辑和风格，不要做任何修改，将来的工作要基于这个脚本进行。





接下来请仿照ccxt\_grid.py的主要逻辑，帮我重写一下ondo平台的中性网格策略并建立ondo\_grid.py文件，主要改动为下单和撤单使用批量接口（例如 POST /v1/perps/orders/batch 或 DELETE /v1/perps/orders/batch），以符合api速率限制，如果超过限制，API 会返回 HTTP 429 错误。

主要要求如下：

1.将SYMBOL映射为平台内的symbol格式。

2\. 代码要简洁易读，减少微型辅助函数的封装，尽量扁平化。

3.readme.me文件不要动，其他都可以动，最后废弃ccxt\_grid.py文件。

4\.不用专门写速率控制的逻辑，因为按0.8s的轮询间隔来说，速率够用了。ondo官方速率为： 持续：2 请求/秒，突发：最多 50 个。



目前 Perps API 默认速率限制 是按账户计算的：
下单（order placement）
• 持续：2 请求/秒
• 突发：最多 50 个
撤单（order cancellation）
• 持续：2 请求/秒
• 突发：最多 50 个
下单和撤单是 两个独立的限制，互不占用。
如果你使用批量接口（例如 POST /v1/perps/orders/batch 或 DELETE /v1/perps/orders/batch），整个批量请求只算一次 API 调用，因此高频策略建议使用批量提交或撤单，可以显著降低请求数量。
如果超过限制，API 会返回 HTTP 429 错误。

