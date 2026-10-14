# 云服务器只读检查记录

检查时间：2026-10-14 00:11（北京时间）。通过 SSH 以 `ubuntu` 登录；本次没有安装软件、上传机器人、修改配置、开放端口或重启服务。登录密码未写入项目配置或本记录。

## 系统与资源

| 项目 | 实测结果 |
| --- | --- |
| 公网 IPv4 | 43.139.142.142 |
| 操作系统 | Ubuntu 22.04 LTS，x86_64 |
| 系统 Python | 3.10.12 |
| Docker / Compose | 已安装并运行；Compose v2.27.1 |
| Nginx | 已安装并运行；nginx -t 通过 |
| Caddy / Certbot | 当前 PATH 中未找到 |
| 内存 | 总计约 3.3 GiB，可用约 1.6 GiB；Swap 2 GiB |
| 根磁盘 | 总计 40 GiB，可用约 18 GiB |
| 时区 / 时间同步 | Asia/Shanghai；NTPSynchronized=yes |

资源快照适合机器人初期运行、调用外部多模态/聊天模型；不表示适合在这台机器上部署本地大模型。

## 已有业务

Docker 运行中容器：

- `ai-customer-service-business-api-1`：映射本机 `127.0.0.1:8001`。
- `ai-customer-service-knowledge-base-1`：映射本机 `127.0.0.1:8010`。
- `ai-customer-service-ai-reply-1`：容器内 8020，无宿主机公网端口映射。

Nginx 已启用：

- `ai-customer-service`：80 端口，server_name 为公网 IP；含 `/account-admin/`、`/api/`、`/ws/`、`/kb-api/`、`/healthz` 路由。
- `personal-site`：80 端口默认站点；server_name 包含 `iwg.wang`、`www.iwg.wang`。

从服务器进行 DNS 查询时，`iwg.wang`、`www.iwg.wang` 均返回 `43.139.142.142`。用户已确认该域名属于自己，并表示备案可能尚未完成、外部通常只能通过 IP 访问。确切备案状态尚未通过云控制台核实。

外部 GET 复核：公网 IP 根路径返回 200，页面标题为 `Welcome to nginx!`；`http://iwg.wang/` 返回 302，跳转至 `https://dnspod.qcloud.com/static/webblock.html?d=iwg.wang`。这确认域名访问进入腾讯云拦截页面，与备案/接入备案问题的现象吻合，具体状态需从云控制台核实。一次 HEAD 请求曾返回 200，因此判断以随后不跟随跳转的 GET 结果为依据。`https://iwg.wang/` 未能建立连接，与服务器没有 443 监听和 UFW 443 放行规则的检查结果一致。

## 端口与防火墙

SSH 22、HTTP 80 已监听；443 未监听；8000 未监听。UFW 已启用，只列出了 OpenSSH、80/tcp 的 IPv4/IPv6 放行规则。

因此 HTTPS 部署还需配置证书、443 监听和 UFW 443 放行。云厂商安全组没有通过 SSH 检查到，需另行确认是否允许 443 入站。

## 建议部署方式

复用现有 Nginx，机器人单独使用 Docker 的 Python 3.12 镜像，宿主机只绑定 `127.0.0.1:8000`。本项目要求 Python 3.11+，不直接使用系统 Python 3.10，也不替换现有系统 Python。仅运行一个机器人进程/副本。

若用户能管理 `iwg.wang` 的 DNS，可新增专用子域名（例如 `bot.iwg.wang`），使用独立 Nginx 站点与证书，转发 `/webhooks/qq`，避免占用已有业务路径。子域名为建议值，尚未配置或验证。

没有可管理域名时，也可考虑公网 IP HTTPS。Let's Encrypt 已于 2026-01-15 开放 IP 地址证书，证书有效期为 160 小时；应部署自动续期和 Nginx 证书重载。Certbot 5.4+ 的 webroot 方式支持 IP 地址证书，可以复用现有 80 端口服务，不必停掉 Nginx。QQ 后台是否接受最终 IP 回调地址还需要实际保存验证，不能仅凭证书签发成功认定接入完成。

来源：[Let's Encrypt IP 证书正式开放](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[Certbot IP 证书用法](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

腾讯云文档说明了公网 IP 直接访问测试、域名解析到境内资源的备案要求等不同情形；当前已有域名解析不能仅通过改用 HTTPS 或 IP 证书认定备案问题已解决。正式服务前应核实云控制台备案与接入状态。来源：[是否需要备案](https://cloud.tencent.com/document/api/243/19630)。

本次检查完成后所有 SSH 会话已退出。机器人和 HTTPS 均未部署到云服务器；本地项目原有模拟服务与云服务器无关。
