# kavita-bangumi-sync

从 [Bangumi](https://bgm.tv/) 抓取漫画元数据，通过 REST API 写入 [Kavita](https://github.com/Kareadita/Kavita)。

## 功能

- 搜索 Bangumi 匹配 Kavita 中的每个系列
- 写入：简介、评分、标签、作者、Bangumi 链接
- 支持 dry-run 预览模式
- 支持单系列同步
- 写入后锁定字段，防止 Kavita 扫描时覆盖

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
```

## 依赖

仅使用 Python 标准库（urllib），无需 pip install。
