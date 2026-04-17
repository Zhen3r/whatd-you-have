# whatd-you-have · 吃了啥

微信饮食追踪 bot：发饭 → Kimi 视觉识别 → 每晚汇总 → 不发饭就花式催。

## 组件

- **[wechatbot-sdk](https://pypi.org/project/wechatbot-sdk/) (iLink 协议)** 扫码登录后长轮询接收消息，自动管理 `context_token`、AES-128-ECB CDN 加解密
- **Kimi / Moonshot 视觉 API** 识别食物、估算热量 / 蛋白 / 脂肪 / 碳水
- **SQLite** 存用户、餐食、催促状态
- **APScheduler** 每日 21:00 发总结；每 15 分钟扫一次催促队列
- **Nagging**：4 档渐进式话术，从温柔提醒到完全癫狂（见 `src/whatd_you_have/nagging.py`）

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # 填入 KIMI_API_KEY
whatd-you-have
```

首次启动会在日志里打印一个二维码 URL，用微信扫码登录。凭证保存在 `WECHATBOT_CRED_PATH`（默认 `./data/wechatbot_credentials.json`），之后重启免登录。

## 关键外部接口（已查文档）

- **iLink**：通过 `wechatbot-sdk` 0.2.0，`@bot.on_message` 收消息，`bot.reply(msg, text)` 回消息，`bot.send(user_id, text)` 主动推送（需该用户先和 bot 说过话，否则 `NoContextError`），`bot.download(msg)` 自动解密下载图片。
- **Kimi 视觉**：`POST {KIMI_BASE_URL}/chat/completions`，Bearer 鉴权。图片必须是 base64 data URL（Kimi 目前不支持公网 URL）。推荐模型 `kimi-k2.5`（当前主推，支持视频），兼容 `moonshot-v1-{8k,32k,128k}-vision-preview`。

## 用户指令

- 发图：自动识别并记录
- 发文字：当作饮食描述
- `/today` 或 `今日`：当日已记录
- `/help`：帮助

## 催促策略

- 上一餐超过 `NAG_AFTER_HOURS`（默认 5h）触发
- 每次间隔至少 `NAG_INTERVAL_MINUTES`（默认 45 min）
- 仅 `NAG_START_HOUR`–`NAG_END_HOUR` 内发送（默认 08:00–23:00）
- 用户一发饭，`nag_level` 重置为 0
- 如果用户从未和 bot 聊过，无法主动推送（SDK 需要 `context_token`）

## 部署

- 单进程：`whatd-you-have`（SDK 长轮询 + 同一 event loop 里跑 APScheduler）
- 不要用多 worker——APScheduler 和 SDK 都是 stateful
- 凭证和 DB 都在 `./data/`，挂载持久卷即可

## 数据

SQLite：`./data/whatd_you_have.db`，三张表 `users` / `meals` / `nag_state`。
