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
# 必需：Kavita 凭据
cp config.example.json config.json
# 编辑 config.json，填入 Kavita 用户名和密码

# 可选：手动指定 Kavita 系列 → Bangumi subject id 的映射（详见下文）
cp overrides.example.json overrides.json
```

## 使用

```bash
# 预览模式（不实际写入）
python3 sync.py --dry-run

# 日常批量同步（推荐）：只写入高置信度匹配，避免误配
python3 sync.py --strict

# 同步所有系列（会把搜索第一条也写进去，命中率高但可能误配）
python3 sync.py

# 只同步指定系列
python3 sync.py --series "链锯人"

# 强制覆盖已同步的系列
python3 sync.py --force

# 同时更新指定系列的系列封面和各卷封面
python3 sync.py --series "链锯人" --cover --cover-volumes

# 事后审计上次同步，列出低置信度匹配和未找到的条目
python3 sync.py --review

# 批量修正：只处理 overrides.json 里列出的系列
python3 sync.py --overrides-only            # 只同步还没 sync 的 override 条目
python3 sync.py --overrides-only --force    # 所有 override 条目全部覆盖同步
```

## 参数

| 参数 | 作用 |
|---|---|
| `--dry-run` | 预览要写入什么，不实际修改 Kavita |
| `--series "名称"` | 只处理名称包含该子串的系列（其他 flag 的筛选也走它） |
| `--force` | 即使该系列已同步过（webLinks 里有 bgm.tv 链接）也重新抓取并覆盖 |
| `--strict` | 只接受 exact / partial 匹配；跳过 Bangumi 搜索第一条的"低置信度"结果 |
| `--cover` | 用 Bangumi 封面替换系列封面（需 `--series`，自动走 strict 搜索） |
| `--cover-volumes` | 用 Bangumi 单行本封面替换各卷封面（需 `--series`，可与 `--cover` 同时传） |
| `--review` | 不联网，读取 `last_sync_results.json` 列出需人工核对的条目 |
| `--overrides-only` | 只处理 `overrides.json` 里列出的系列（可与 `--force`、`--dry-run`、`--series` 组合） |

### `--strict` 详解

Bangumi 搜索在 exact / partial 都不命中时，默认会退回搜索结果第一条（输出标记 `[first]`），这常常是不相关的条目。带 `--strict` 后遇到这种情况直接跳过，该系列标为"未找到"——可以事后用 `--review` 看清单，再在 `overrides.json` 里手动指定正确的 subject id。

典型工作流：

```bash
python3 sync.py --strict                      # 第一遍：只写高置信度
python3 sync.py --review                      # 看哪些被跳过 / 未找到
# 编辑 overrides.json 加入正确映射
python3 sync.py --overrides-only              # 批量把新加的 overrides 写入 Kavita
```

加了 overrides 后如果想重新覆盖已有数据（比如原来匹配错了），用 `--overrides-only --force`。该模式下还会检查 `overrides.json` 的 key 是否都能在 Kavita 找到对应系列——不能匹配的会打印警告，通常意味着拼写错了或 Kavita 那边重命名过。

## 手动映射 (`overrides.json`)

当 Bangumi 自动搜索找不到或匹配错时（比如 `--review` 列出的条目），可以在 `overrides.json` 里直接指定系列 → subject id 的对应关系：

```json
{
  "_comment": "可选注释（任何 value 非整数的 key 会被忽略）",
  "海贼王": 3510,
  "灌籃高手": 36752,
  "只想告訴你": 5944
}
```

- **key**：Kavita 里系列的名字，要与 Kavita UI 完全一致（包括繁简、空格）
- **value**：Bangumi subject id（整数）

找 subject id 的方法：在 [bgm.tv](https://bgm.tv) 搜到作品打开，浏览器地址栏
`https://bgm.tv/subject/3510` 里的 `3510` 就是。

命中 override 的系列会跳过自动搜索直接抓取该 subject，置信度标记为 `[映射]`，优先级最高。

## 依赖

- Python 3.6+，仅依赖标准库（urllib）
- 可选：`pip install opencc`。安装后会把繁体标题转简体再搜索，提高命中率（台/港版漫画尤其明显）
