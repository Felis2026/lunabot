# LunaBot 部署指南（维护分支补充）

> 本文档是维护分支的部署补充说明，请先阅读 `README.md`。  
> 如果原文档与本文档冲突，以本文档的“当前仓库结构与模板文件”说明为准。

## 1. 适用范围

- 适用于当前仓库的 Docker 部署。
- 目标是“可启动、可连接 NapCat、可运行基础功能”。
- Sekai 相关高级功能依赖额外资源与外部 API，见下文“可选能力”章节。

## 2. 前置要求

- Docker Desktop（建议新版，含 Compose）。
- 可正常拉取 Docker 镜像。
- Windows / Linux 均可（下文命令以 PowerShell 为主）。

## 3. 初始化步骤

### 3.1 克隆仓库

```powershell
git clone <your_repo_url> your_bot_dir
cd .\your_bot_dir
```

### 3.2 生成本地 compose 文件（不要直接改 example）

```powershell
Copy-Item .\docker-compose.example.yml .\docker-compose.yml
```

然后编辑 `docker-compose.yml`：

- `napcat.environment.ACCOUNT` 改成你自己的 QQ 号。
- `napcat.mac_address` 改成你自己的示例值（避免冲突）。
- 如有需要可调整网络名、端口映射。

### 3.3 生成环境变量文件

```powershell
Copy-Item .\.env.example .\.env
```


### 3.4 生成配置目录

```powershell
New-Item -ItemType Directory -Force .\config | Out-Null
Copy-Item .\example_config\* .\config\ -Recurse -Force
```

然后按需编辑 `config` 下配置。

## 4. 必填/建议填写项

## 4.1 最低建议（保证基础可用）

- `config/global.yaml`
  - `superuser`：改成你自己的 QQ 号。
  - `font.path`：默认是容器内路径，保持默认即可（需挂载 `fonts`）。

## 4.2 Sekai 功能相关（不用可先不配）

- `config/sekai/sekai.yaml`
  - `gameapi_token`：接入你的游戏 API token。
- `config/sekai/gameapi.yaml`
  - 各 API URL 需替换 `xxx` 占位地址。
- `config/sekai/asset.yaml`
  - 资源源地址需替换成你可用的数据源。

## 4.3 其他可选服务

- LLM、图搜、邮件等功能的 `api_key` / SMTP 配置在对应 yaml 文件中，默认均为占位值。

## 4.4 Sekai 账号云同步（可选）

如果你部署了配套的 Sekai 账号云端 API，可以让多个 bot 实例共享 `sekai` 账号状态。

可选环境变量如下：

```env
SEKAI_ACCOUNT_API_BASE_URL=https://your-account-api.example.com
SEKAI_ACCOUNT_API_TOKEN=replace_me
SEKAI_ACCOUNT_API_TIMEOUT=10
SEKAI_ACCOUNT_CACHE_TTL=120
```

说明：

- 如果这些环境变量未配置，bot 会继续使用本地 `data/sekai/profile/db.json`。
- 如果你有多实例（例如 `bot_a` / `bot_b`）并希望共享 `sekai` 账号数据，再考虑启用该能力。

相关管理命令：

- `/pjsk blacklist add [cn|jp] <uid> [reason]`
- `/pjsk blacklist remove [cn|jp] <uid>`
- `/pjsk blacklist list`
- `/pjsk qid blacklist add <qid> [reason]`
- `/pjsk qid blacklist remove <qid>`
- `/pjsk qid blacklist list`

## 5. 启动

```powershell
docker compose -f .\docker-compose.yml up -d --build
```

查看状态：

```powershell
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

查看主容器日志：

```powershell
docker logs --tail 200 lunabot_container
docker logs --tail 200 napcat_container
```

## 6. 运行结构说明

- `start.sh` 会在主容器中启动：
  - autochat 服务
  - deck recommender 服务
  - event tracker 服务
  - 最后启动 `nb run`
- 这意味着配置缺失时，可能出现部分功能报错，但不一定影响主进程启动。

## 7. Sekai 资源说明（可选能力）

- 仓库不包含完整 `data/sekai` 资源。
- 若要完整启用 Sekai 绘图/素材功能，需要你自行准备资源目录（如 `data/sekai/assets/...`）。
- 你可以使用独立的资源更新流程（例如外部 asset-updater）来填充数据目录。

## 8. 常见问题

### 8.1 首次构建很慢

- 正常现象。`Dockerfile` 会安装系统依赖、Python 依赖与 Playwright 浏览器。

### 8.2 图片渲染中文方块

- 确保项目根目录存在 `fonts`，并包含可用中文字体（例如 Microsoft YaHei）。
- compose 已将 `./fonts` 挂载到容器 `/root/.fonts`。

### 8.3 新版 emoji / pilmoji 兼容性

- 当前仓库已经内置了对新版 `emoji` 依赖的兼容处理。
- **不需要**再手动去修改 `site-packages/pilmoji/helpers.py`。
- 如果你参考了旧教程中“手改 `pilmoji` 源码”的步骤，请忽略那部分旧说明。


## 9. 升级流程（建议）

```powershell
git pull
docker compose -f .\docker-compose.yml up -d --build
```

如结构有变化，先重新对比 `docker-compose.example.yml` 与 `example_config/` 新增项。
