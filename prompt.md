我又胖了5斤。写一个用 Python 构建的饮食追踪 bot: 吃了啥（whatd-you-have），运行在微信（通过 wechatbot.dev 的 iLink API）上。用户发送三餐照片或描述后，bot 调用 Kimi API 的视觉能力识别食物并估算热量和营养素，每天晚上推送当日饮食总结。如果用户超过一定时间没有上传餐食，bot 会持续发送预先生成好的一堆催促消息轰炸用户，消息风格荒诞幽默、越来越癫。

---

如果有任何api，字段，模型，id，等不确定，必须查找文档，直接写出能用的代码。

---

识别失败：Kimi API 400: {"error":{"message":"invalid temperature: only 1 is allowed for this model","type":"invalid_request_error"}}

---

1. 现在我发图片的时候会直接请求，我要改成："当用户接下来xxx s内没有发送文字消息"，才请求。
2. 用户应该有这个能力，去引导模型修改前面的记录。要怎么实现？ 

