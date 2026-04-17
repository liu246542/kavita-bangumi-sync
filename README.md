# kavita-bangumi-sync

从 [Bangumi](https://bgm.tv/) 抓取漫画元数据，通过 REST API 写入 [Kavita](https://github.com/Kareadita/Kavita)。

## 效果预览

![同步后的系列在 Kavita 中的展示](docs/screenshot.webp)

简介、Bangumi 评分/排名、作者、标签自动填充；卷封面替换为 Bangumi 单行本封面。

## 功能

- 搜索 Bangumi 匹配 Kavita 中的每个系列
- 写入：简介、评分、标签、作者、Bangumi 链接
- 可选替换系列封面和每卷封面为 Bangumi 原图
- 支持 dry-run 预览模式
- 支持单系列同步
- 写入后锁定字段，防止 Kavita 扫描时覆盖
- `--review` 事后列出低置信度匹配，便于人工核对

## 配置

```bash
cp config.example.json config.json
# 编辑 config.json，填入 Kavita 用户名和密码
```

## 使用

```bash
# 预览模式（不实际写入）
python3 sync.py --dry-run

# 同步所有系列
python3 sync.py

# 只同步指定系列
python3 sync.py --series "链锯人"

# 强制覆盖已有数据
python3 sync.py --force

# 同时更新指定系列的系列封面和各卷封面
python3 sync.py --series "链锯人" --cover --cover-volumes
```

## 依赖

- 仅依赖 Python 标准库（urllib）
- 可选：`pip install opencc`。安装后会把繁体标题转简体再搜索，提高命中率（台/港版漫画尤其明显）
