# OpenClaw 培训结果

这次培训完成了 3 件事：

1. 统一主模型为 `minimax/MiniMax-M2.5`
2. 给主 agent 补了 fallback：
- `moonshot/kimi-k2.5`
- `volcengine-plan/doubao-seed-2.0-pro`
3. 把主 agent 工具档位提升到 `coding`，并保留 `exec`

## 已安装的新技能

- `chenzong-autopilot`
- `boss-recruiting-assistant`
- `xiaohongshu-growth-assistant`
- `web-form-automation`
- `lead-backoffice-ops`

## 已补齐的新模板

- `video-editor-assistant`（技能说明已写好）
- `video-editor-worker`（本地工作区模板已补齐，可接成 TG 剪辑师 worker）

## 明早直接怎么用

### 1. 总控模式

直接对 OpenClaw 说：

```text
使用 chenzong-autopilot，帮我处理今天最优先的自动化任务。
```

### 2. 招聘模式

```text
使用 boss-recruiting-assistant，帮我读 Boss 直聘未读消息，先提炼重点，再给出待发送回复稿。
```

### 3. 小红书模式

```text
使用 xiaohongshu-growth-assistant，帮我拆这条笔记的标题、结构、评论机会，再给我 3 个新选题。
```

### 4. 网页与表单模式

```text
使用 web-form-automation，帮我打开这个网页，先读取结构，再告诉我下一步该怎么填。
```

### 5. 后台与线索模式

```text
使用 lead-backoffice-ops，帮我看后台新增线索，标记重点和异常项，再给我导出建议。
```

### 6. 剪辑师模式

```text
使用 video-editor-assistant，帮我先判断这个题值不值得做，再直接出适合剪映落地的口播稿、字幕、封面文案和剪辑节奏。
```

## 当前训练原则

- 默认先自动整理和起草
- 高风险动作先确认再执行
- 重要网页任务先读页面，再操作
- 浏览器自动化失败时，立即切换手动指导
