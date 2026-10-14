# QQ 订单机器人服务端

基于 FastAPI + QQ WebSocket/Webhook + SQLite，接收 QQ 单聊和群聊消息，提取订单、累计消费并持续生成 Excel。当前账号后台已确认选择 WebSocket，采用服务器主动连接 QQ 的方式接收消息。

当前进度和后续工作见 [项目状态记录](docs/project-status.md)。截至 2026-10-14，本地 QQ 沙箱 WebSocket 的真实消息收发、DeepSeek 视觉调用和图片确认入账已验证；客户 QQ 号、订单淘宝账号名与缺项补充流程已实现。云端尚未部署。WebSocket 接入无需自己的域名、服务器证书或公网 HTTPS 回调。

## 平台能力与实现边界

- QQ 官方文档已注明：**2025-04-21 起停止主动推送**。订单变更话术有可用回复窗口时发送，否则入待通知队列，客户再次发消息后补发。不能保证在后台半月核对后随时主动私发 QQ。
- 官方文档列出的被动回复窗口为群聊 5 分钟、单聊 60 分钟，每条消息最多 5 次回复。本项目分别采用 240 秒、3300 秒的保守窗口，并跟踪回复次数。
- 默认通过群聊 `GROUP_AT_MESSAGE_CREATE`（@机器人）及单聊 `C2C_MESSAGE_CREATE` 接入。支持处理后台实际下发的 `GROUP_MESSAGE_CREATE`，全量群消息权限是否开放以账号后台为准。
- 旧官方事件文档的 WebSocket 下线提示与当前后台、腾讯新 SDK 存在差异；用户提供的后台截图已确认本账号可用且当前选择 WebSocket。以本账号实际能力和联调结果为准，保留 Webhook 作为可选方式。
- 没有真实电商接口时，每 14 天生成全量待核对清单，支持最新店铺 CSV 导入和人工核验。不会仅凭过期截图宣布退款、收货状态已更新。
- 多模态服务应支持 `chat/completions`、JSON 模式和 `image_url`。本项目直接接入可配置的兼容协议，不包含某个 OCR 厂商专用 SDK。

参考：[腾讯官方发送消息文档](https://github.com/tencent-connect/bot-docs/blob/master/docs/develop/api-v2/server-inter/message/send-receive/send.md)、[官方 Webhook 实现](https://github.com/tencent-connect/botgo/blob/master/interaction/webhook/webhook.go)、[官方验签实现](https://github.com/tencent-connect/botgo/blob/master/interaction/signature/interaction.go)、[官方 botpy](https://github.com/tencent-connect/botpy)。网页版文档无法抓取的部分使用了腾讯维护的文档源码和 SDK 源码核实。

## 本地启动（Windows PowerShell）

要求 Python 3.11+，推荐 3.12。本目录已创建 `.venv` 并安装验证依赖；换机器时先安装：

```powershell
cd D:\qq-bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\init_env.py
```

`init_env.py` 仅在不存在 `.env` 时创建它，并生成随机管理员令牌，不覆盖已有配置。QQ 凭据本次已填写；新环境需安全同步配置。凭据保存在本地，不要提交或贴到聊天中。先保持 `QQ_GATEWAY_ENABLED=false`、`QQ_DRY_RUN=true` 进行离线检查，准备真实联调时再启用网关和真实发送。

```powershell
.\.venv\Scripts\python.exe -m uvicorn qqbot.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [管理 API 页面](http://127.0.0.1:8000/docs)，点击 **Authorize** 输入 `.env` 中的 `ADMIN_TOKEN`（只输入令牌，不加 Bearer 前缀）。页面可查询客户、订单、待确认记录、核对清单、通知和审计，确认订单、导入 CSV 或下载 Excel。健康检查为 `/healthz`。

本次搭建已在后台启动本地模拟服务。修改配置后可先运行 `powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1` 停止这次记录的服务，再运行上方 uvicorn 命令重启。停止脚本会核实进程可执行文件及命令行，避免操作已被其他程序复用的 PID。

**仅运行一个进程和一个副本**。当前版本使用 SQLite 和进程内任务锁，多 worker 会重复消费任务；不要改为 `--workers 2` 或在同一数据卷上启动多副本。后台任务依赖服务持续运行，重启会恢复持久队列和逾期的定时核对。

## QQ 平台接入

当前采用 **WebSocket**：

1. QQ 后台“事件订阅与回调”保持当前 WebSocket 选项，确认群聊、单聊和沙箱权限及使用范围。
2. 将本项目部署到 Ubuntu，使用 Docker/Python 3.12、单进程、持久化数据目录。管理 HTTP 端口只绑定 `127.0.0.1:8000`。
3. 核实调用 QQ API 的实际公网出口 IP，并按后台配置服务器 IP 白名单。WebSocket 方式仍需要凭据与相应权限。
4. 准备接收真实 QQ 消息时，将 `.env` 设置为：

   ```dotenv
   QQ_TRANSPORT=websocket
   QQ_GATEWAY_ENABLED=true
   QQ_DRY_RUN=false
   QQ_GATEWAY_INTENTS=33554432
   ```

5. 启动或重启服务，访问管理 API `/admin/status`，确认 `gateway.state=ready`、`gateway.ready=true`。`disabled` 表示未启用；`reconnecting` 表示重试中；`fatal` 表示需要处理鉴权、权限、分片或重试耗尽问题后重启，不能当成已经上线。
6. 用沙箱单聊和群 @先发送“帮助”、完整文字订单，再验证真实图片模型。实际订阅的是群/单聊对应 intent 位，默认不请求全部事件权限。

服务器通过出站 HTTPS 获取 token 和网关地址，通过出站 WSS 保持连接；WSS 的证书由 QQ 网关提供，本程序使用系统 CA 验证。无需在本机配置入站 443、自有域名或证书。网关不默认使用系统代理；部署时核实真实出站网络和白名单。

已实现 Hello、Identify、心跳 ACK、Resume、退避重连和会话恢复。业务事件与恢复序号在同一数据库事务中保存；权限错误和网关禁用不会无限重试。当前只支持一个网关连接、一个分片，服务端要求多个分片时会明确停止并报告，需要另行扩展。网关 session 按 AppID、API 环境和 intents 隔离。

`QQ_GATEWAY_ENABLED` 与 `QQ_DRY_RUN` 独立：前者控制是否接收真实事件，后者控制是否发送真实回复。启用网关并保持 dry_run 会真实收消息、写订单，只模拟回复，不应误认为完全离线。相同机器人不要同时在本地和云端启动多个活动接收实例。

若后续选择 **Webhook**，设置 `QQ_TRANSPORT=webhook`、`QQ_GATEWAY_ENABLED=false`，再部署公网 HTTPS `/webhooks/qq` 回调。此时才需要自己的 HTTPS 入口；保留原有验签和地址验证能力。本地 `127.0.0.1` 不能作为公网回调。

若账号文档指定不同的 API 域名，可覆盖 `QQ_API_BASE`，沙箱环境同样按后台要求设置。参考：[腾讯当前 SDK 的 WebSocket 接入](https://github.com/tencent-connect/qqbot-nodejs/blob/main/USAGE.md)、[网关地址接口](https://github.com/tencent-connect/bot-docs/blob/master/docs/develop/api-v2/openapi/wss/shard_url_get.md)。

## 图片识别和 AI 陪聊

在 `.env` 设置：

```dotenv
MODEL_BASE_URL=https://api.deepseek.com
MODEL_API_KEY=你的密钥
VISION_MODEL=deepseek-flash
CHAT_MODEL=deepseek-flash
CHAT_ENABLED=true
```

这里使用 DeepSeek 当前推荐的 `deepseek-flash`（V4.1 Flash，支持视觉）；旧名称
`deepseek-v4-flash-vision-exp` 已下线，目前仅作为兼容别名临时路由到最新 Flash 模型。

识别订单号、店铺、日期、下单金额、实付款、状态和累计已到账退款额。只接收白名单 QQ CDN 的 HTTPS 图片，不跟随跳转，限制下载大小、像素数和格式。真实附件使用了新的 CDN 域名时，在核实其归属后补充 `IMAGE_ALLOWED_HOSTS`。

图片以 base64 发送给你配置的外部模型，并保存本地原图供核验。缺少字段、冲突、模型输出不合法或识别失败均进入待确认清单，不用猜测值填账。未配置模型时图片转人工核验，完整模板文字仍可自动入账。

默认 `AUTO_APPLY_AI=false`：AI 结果进入 Excel 的“待确认识别”，客户看到字段预览后发送 `/确认 编号` 或由管理员更正后确认。确认前不计入消费。根据自己的截图样本验证后，可设为 `true`，让无缺失、无冲突且满足 `AI_CONFIDENCE_THRESHOLD` 的识别结果自动入账。模型自报置信度不是准确率保证。

陪聊默认关闭；开启后仅处理非订单消息，按单轮上下文回答，不授予模型修改订单或查询其他客户的能力。

## 客户使用

完整文字模板会自动入账，字段顺序不限；QQ号由客户提供，登记后同一聊天身份可复用，淘宝账号名按每笔订单填写：

```text
QQ号：123456789
淘宝账号名：示例买家账号
店铺：示例旗舰店
订单号：00012345678901234567
下单日期：2026-10-01
下单金额：129.00
实付款：99.90
订单状态：待收货
```

图片或文字缺少信息时先保存资料，缺失值保持空白。机器人列出待补充项，不计入累计消费。
同一客户只有一条待处理记录时可直接发送缺项；有多条时在首行指定编号：

```text
/补充 3
QQ号：123456789
淘宝账号名：示例买家账号
下单金额：129.00
```

补充值与已有内容不一致时不自动覆盖，使用 `/更正 3`，下一行填写正确值。
补充后展示完整中文预览，再由客户 `/确认 3` 入账；图片默认也需确认。
退款中、部分退款必须补充累计已到账退款额，尚无退款到账应明确填写 `0`。
客户提供相同 QQ号不会自动合并不同 OpenID 的账本。已登记 QQ号有误时由管理员核实，
通过 `PATCH /admin/customers/{customer_id}` 提交 `qq_number` 和 `verification_note` 更正并留审计。
淘宝账号名不会自动从其他订单沿用，已有订单的更新会保留其原账号名。

更新已有订单可只发变更字段，未提供的字段保留：

```text
订单号：00012345678901234567
店铺：示例旗舰店
订单状态：部分退款
退款金额：20.10
```

退款金额必须为**累计已到账额**，重复发送同一退款不会再次扣减。全额退款可发“订单状态：已退款”，系统将累计退款设为实付款；部分退款须提供具体金额。多个店铺使用相同订单号时必须带店铺。

指令：`帮助`、`/消费`、`/补充 123`、`/更正 123`、`/确认 123`、`/拒绝 123`。
补充和更正指令下一行填写资料或附图。订单截图并非电商交易真实性证明，客户确认与管理员/店铺权威核验时间分别记录。

## 消费口径、身份与 Excel

金额先转换为整数分计算。有效消费 = 实付款 − 累计已到账退款；待付款、已取消、已退款为零。退款中仍按已到账退款扣减，申请退款不会提前清零。待发货、待收货、已完成均计入。

去重键为**店铺 + 订单号**。重复截图、重复 QQ 回调和重复退款不会重复入账。相同订单尝试归到另一客户时阻止并转人工核验。保留订单版本和前后数据审计。

单聊 OpenID 与群成员 OpenID 默认视为独立身份，群身份还包含群 ID。不能按昵称或假设 OpenID 一样来合并。管理员核实属于同一个 QQ 用户后，调用 `/admin/identities/link` 合并客户，所有订单一起参与累计消费并留下依据。群聊发送消费数字时该群其他成员可见；需要私密查询应使用单聊。

Excel 自动写入 `data/reports/orders.xlsx`，包含：

- 客户汇总：客户提供的 QQ号、订单数、实付款、退款和累计有效消费。
- 订单明细：QQ号、每笔订单的淘宝账号名、订单字段、状态、退款、数据来源和核验时间。
- 待确认识别：分列展示已有信息、待补充项和处理提示；不展示原始 JSON，缺失值留空。
- 半月核对：缺少权威数据或同步失败的订单。
- 待发送通知：等待窗口或发送失败的话术。

Excel 是数据库业务快照，支持排序和筛选；直接编辑 Excel 不会回写数据库。更正请用管理 API 或导入店铺 CSV。后台以临时文件和原子替换刷新报表；Windows Excel 占用文件时保留数据库变更、记录导出错误并重试，关闭 Excel 后即可恢复。

报表不展示 QQ昵称或内部客户 ID。旧订单未登记的 QQ号、淘宝账号名保持空白，可由客户补充后更新；首次启动会执行可重复的加列迁移，保留原账本。

## 每 14 天全面核对

服务运行时每 14 天检查全部已登记订单，并在重启后补跑逾期任务。有授权店铺查询网关时更新真实状态和退款，没有时维护待核对清单。每笔核验有独立结果；全部成功才记录“最近一次全量核对完成时间”。人工导入只表示文件内成功匹配的订单已核验，文件里未出现的订单不会自动改状态或标成已核验。

管理 API `POST /admin/reconcile` 可立即触发核对。`POST /admin/import/csv` 支持 UTF-8 CSV，必需列为“订单号、店铺、订单状态、退款金额”，未退款填 0。可选日期、下单金额、实付款；模板见 `examples/store-orders.csv`。按行提交并返回错误清单，可安全重传。未知订单不会自动猜测客户归属。

店铺 API 接入方式见 [commerce-adapter.md](docs/commerce-adapter.md)。真实接口文档和授权尚未提供，因此当前没有真实电商平台自动同步。

## 自动话术与失败处理

默认话术位于 `qqbot/ledger.py` 的 `CHANGE_TEMPLATE`，管理员可修改再重启。例如：

> 已帮您更新这笔订单啦！
>
> 示例旗舰店  
> 订单号：00012345678901234567  
> 当前状态：部分退款
>
> 本笔计入消费：79.80 元  
> 累计有效消费：159.80 元  
> 本次消费变动：-20.10 元

每个订单版本只创建一条通知，另有客户指令回复。跟踪 `sent`、`simulated`、`waiting`、`failed`、`pending` 状态；网络及限流按退避重试，固定权限错误保留失败。相同重试保留 msg_id + msg_seq，降低重复发送风险。网络超时仍可能出现“平台已收但客户端未确认”，需人工核实结果，不承诺外部消息严格只送达一次。

超出回复窗口或 5 次回复额度的通知等待下一条客户消息。已分配序号但发送结果不明、且原窗口过期的记录保留为 waiting 供人工核实，避免自动换序号重复投递。失败事件和失败通知可通过管理 API 重试。

## 部署与数据保存

提供 `Dockerfile` 和 `compose.yaml`，可用 `docker compose up -d --build` 启动。当前 WebSocket 模式不需要改现有 Nginx、申请证书或开放入站 443。Docker 已排除 `.env` 和数据，不会把密钥打入镜像。Linux 数据卷目录要允许容器用户写入。

需要从电脑访问云端管理接口时，可使用 SSH 端口转发，例如 `ssh -N -L 18000:127.0.0.1:8000 ubuntu@43.139.142.142`，随后访问 `http://127.0.0.1:18000/docs`。管理接口继续使用 ADMIN_TOKEN，HTTP 连接由 SSH 隧道传输，不公开暴露管理端口。

`data/` 包含 SQLite 账本、订单证据图片和 Excel，需放在持久磁盘。备份 SQLite 请使用在线 backup API 或停机后整体备份，不能运行中只复制主数据库文件而遗漏 WAL。订单截图会发往所配置模型服务，并可能含个人信息；投入客户使用前应告知处理方式。当前未实现自动证据留存期限清理，按实际业务制定保留期限。

管理接口均需 Bearer 令牌；不要将 ADMIN_TOKEN 放在 URL 中。多租户、跨服务器队列、大规模分页店铺同步和平台专有签名不在此版本内。

## 测试与离线演示

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\demo.py
```

测试覆盖金额精度、退款、重复订单、客户归属、身份合并、Webhook 验签、WebSocket 鉴权/心跳/重连/恢复/原子入队、模型图片提取协议、确认入账、权威核验、CSV 导入、消息回复窗口、回复次数和 Excel 字段类型，另覆盖 QQ登记、缺项补充、冲突更正、跨客户保护、重启恢复和旧账本迁移。自动化测试中的 QQ、模型和店铺 API 使用模拟响应；这不能代替真实账号联调。

离线演示通过真实的服务路由和 worker 处理两个示例订单、重复消息和部分退款，预期累计消费 **159.80 元**。演示使用 `outputs/demo-时间/` 独立数据库，不写入真实 `data/`，不发 QQ，不调用收费模型。
