# 本地运行

项目无需租赁服务器。GitHub 展示源码和截图，本机通过浏览器访问工作台。本项目当前没有公开在线体验地址。

## 环境

- Python 3.13 为本机已验证版本，PostgreSQL 17。
- 创建虚拟环境并安装依赖；已有虚拟环境不必重建。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.lock.txt` 保留已解析的带 hash 依赖，便于进一步锁定环境；不能把依赖解析成功当成所有平台运行验收。

## 推荐先运行无模型费用演示

已有本项目 portable PostgreSQL 的用户先确保现有实例运行；新克隆用户自行安装并启动 PostgreSQL，使用独立本地 bootstrap 连接。

```powershell
# 新安装 PostgreSQL 的示例。密码使用 URL 编码，不把真实值写入仓库。
$env:SHOWCASE_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://postgres:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/postgres'
.\.venv\Scripts\python.exe -m scripts.demo_local start --open
.\.venv\Scripts\python.exe -m scripts.demo_local status
.\.venv\Scripts\python.exe -m scripts.demo_local stop
```

如果已有忽略目录中的本项目 bootstrap 凭据，可不设置上述变量。演示不会下载或安装 PostgreSQL，也不会自动启动其他数据库服务。配置缺失时安全停止，不回退正式库。首次启动需要 bootstrap 账号能够创建演示数据库和受限角色。

演示 API 为 `127.0.0.1:18700`，UI 为 `127.0.0.1:18701`。独立数据库 `aftercare_showcase_demo`，使用专用 app/reader 角色、随机令牌与所有权标记；配置只在忽略的本地目录保存。固定 Clock、FakeLLM、真实 PG 和真实只读 MCP，不调用供应商，不开启云追踪。**FakeLLM 是固定流程脚本，只使用 ORD-1009 体验退货，不支持任意客户问题。**

停止只关闭有本入口实例标识的进程，保留数据；不会关闭已有业务 API、UI 或 PostgreSQL。显式重置前先停止演示：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_local reset --confirm-reset
```

重置会删除本入口拥有且标记匹配的演示库和两个角色，拒绝未确认、在运行、所有权不符或其他数据库。不要在面试途中重置。

## 使用自己的真实模型配置

真实模式仍沿用原启动方式，不与演示配置混用：

1. 根据 `.env.example` 在本地配置兼容模型服务及自己的密钥；不要提交 `.env`。
2. 配置应用数据库和不同 reader 角色；可参考 `scripts.start_api` 的初始化与权限检查逻辑。
3. 在目标库显式运行 Alembic 迁移、生成操作员凭据，按需显式 seed **合成演示库**。启动不自动清空或重新 seed。
4. 本机 Windows 启动 `run.py`。启动检查数据库、迁移、权限和端口，API/UI 就绪后打开一个页面。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.create_operator
.\.venv\Scripts\python.exe run.py
```

原 Windows portable 管理脚本需要自行准备官方 PostgreSQL 程序，不随 Git 仓库打包；`local_pg ensure` 只恢复既有集群，不初始化新数据目录。不要为运行演示而修改原模型、数据库或操作员配置。

## 容器说明

仓库保留 Dockerfile、Compose 与独立验证配置。完整 Docker Linux 容器、数据卷重启仍需实际引擎验收；本项目仅已验证静态配置与本机 PG/MCP/HTTP 链路。容器中的服务名与本机 loopback 不同，不能把 Dockerfile 存在当作部署成功。
