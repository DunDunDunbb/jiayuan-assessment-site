# 北京家圆评估页

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/DunDunDunbb/jiayuan-assessment-site)

这个目录现在包含三个主要部分：

- `index.html`: 前台页面
- `admin.html`: 后台登录页和线索管理页
- `server.py`: 轻量 Python 后端，负责计算结果、保存姓名和电话、提供后台列表和登录鉴权

## 本地运行

```bash
cd /Users/clawbot/Documents/Playground
python3 server.py
```

打开：

- 前台：`http://127.0.0.1:4173/`
- 后台：`http://127.0.0.1:4173/admin`

## 后台功能

- 表单提交后会保存：
  - 姓名
  - 联系电话
  - 提交时间
  - 结果区间
  - 主要评估字段
- 后台支持账号密码登录
- 后台支持导出 CSV
- 后台支持删除记录

后台账号密码不要直接写进仓库。建议放到本地 `.env` 或部署平台环境变量里：

```text
ADMIN_USERNAME=你的后台账号
ADMIN_PASSWORD=你的后台密码
SESSION_SECRET=一串随机字符串
```

## 二维码替换

当前页面会优先读取：

```text
assets/wecom-card.png
```

如果 `png` 不存在，会继续尝试：

```text
assets/wecom-card.jpg
```

如果都不存在，就会显示占位图：

```text
assets/wecom-card-placeholder.svg
```

## 部署准备

- 后端默认把 SQLite 数据库存到 `data/`
- 如果部署平台支持持久化磁盘，可以设置环境变量 `DATA_DIR`
- 正式部署建议设置：
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
  - `SESSION_SECRET`
  - `DATA_DIR`
- 当前目录已包含 `render.yaml`，适合直接部署到 Render 这类支持 Python Web Service 的平台
- 如果你仍然需要兼容旧的口令方式，也可以继续设置 `ADMIN_TOKEN`

## 旧文件

目录里还保留了之前的 `chat.py` 和火山调用示例，不影响当前网页项目运行。
