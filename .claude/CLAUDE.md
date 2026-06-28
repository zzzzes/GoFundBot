powershell是GBK编码，代码是UTF-8，所以用直接用powershell看代码会有中文乱码

# GoFundBot 容器持久化部署

## 架构

```
Host (Mac)                    Container (gofundbot)
~/GoFundBot ── bind mount ──→ /app (代码，实时同步)
gofundbot-data ── volume ──→ /app/Backend/Data (数据库，持久化)
~/.ssh ── bind mount ro ───→ /root/.ssh (Git 认证)
```

## 容器管理

```bash
# 首次启动（容器还在就跳过）
docker rm -f gofundbot
docker run -d --name gofundbot \
  --add-host=host.docker.internal:host-gateway \
  -p 5000:5000 \
  -v ~/GoFundBot:/app \
  -v gofundbot-data:/app/Backend/Data \
  -v ~/.ssh:/root/.ssh:ro \
  -e HTTP_PROXY=http://host.docker.internal:7890 \
  -e HTTPS_PROXY=http://host.docker.internal:7890 \
  -e http_proxy=http://host.docker.internal:7890 \
  -e https_proxy=http://host.docker.internal:7890 \
  -e NO_PROXY='localhost,127.0.0.1,172.17.0.0/16,ports.ubuntu.com,archive.ubuntu.com,security.ubuntu.com,nodesource.com,registry.npmjs.org,pypi.org,deb.nodesource.com' \
  -e no_proxy='localhost,127.0.0.1,172.17.0.0/16,ports.ubuntu.com,archive.ubuntu.com,security.ubuntu.com,nodesource.com,registry.npmjs.org,pypi.org,deb.nodesource.com' \
  -e TZ=Asia/Shanghai \
  ubuntu:24.04 sleep infinity

# 然后初始化（首次自动装依赖，之后秒启动）
docker exec gofundbot bash /app/entrypoint.sh
```

## 日常开发

- **改 Python 代码**：直接编辑 `~/GoFundBot/Backend/` 下的文件，Flask debug 模式自动重载
- **改前端**：`docker exec gofundbot bash -c "cd /app/Frontend && npm run build && cp -r dist/* /app/Backend/static/"`
- **新增依赖**：`docker exec gofundbot pip3 install --break-system-packages xxx`
- **重启服务**：`docker restart gofundbot && sleep 8 && docker exec gofundbot bash /app/entrypoint.sh`
- **看日志**：`docker exec gofundbot tail -f /var/log/supervisor/backend-stderr.log`
- **容器内 commit & push**：`docker exec gofundbot bash -c "cd /app && git add -A && git commit -m 'xxx' && git push origin master"`
- **容器内 git pull**：`docker exec gofundbot bash -c "cd /app && git pull"`
- **Git 配置**：remote=SSH `git@github.com:zzzzes/GoFundBot.git`, user=Zes007, email=zesqaq@gmail.com
- **注意**：`.claude/CLAUDE.md` 被 .gitignore 忽略，需要用 `git add -f` 才能提交

## 重新安装依赖

```bash
docker exec gofundbot rm /app/.initialized
docker restart gofundbot && sleep 5 && docker exec gofundbot bash /app/entrypoint.sh
```

## 代理注意

NO_PROXY 必须包含 ubuntu/npm/pypi 源域名，否则本地镜像源走代理会失败。
mihomo 代理在 `host.docker.internal:7890`。

## 数据持久化

- `gofundbot-data` 是 Docker named volume，删除容器不会丢
- 数据库文件在 `Data/funds.db`
- 缓存文件在 `Data/fund_list_cache.json`
- `docker volume ls` 可以看到，`docker volume rm gofundbot-data` 会彻底删除
