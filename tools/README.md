# PDB 同步工具

这个目录现在同时支持两类流程：

- `main.py`：给单个 URL 列表做并发下载、拉 PDB、导出 JSON
- `sync_symbols.py`：自动下载 Winbindex 的 `*.json.gz`，生成 raw/dedup URL，和远端仓库做差异对比，再通过 `gh api` 直接上传 URL 与新增 JSON

## 模块配置

- `modules.json`：当前同步模块列表
- 新增模块时只需要把模块名加到这里

## 生成 URL

```bash
python tools/winbindex_urls.py ci.dll --catalog ci.dll.json.gz --raw-output out/raw.txt --dedup-output out/dedup.txt
```

如果不传 `--catalog`，脚本会自动下载 Winbindex 的 `ci.dll.json.gz`。

## 单模块并发导出

```bash
python tools/main.py --input url/dedup/fltmgr.sys.url.dedup.txt --workers 5 --final-output-root out
```

## 全量增量同步

```bash
python tools/sync_symbols.py --repo x500x/winKernelPdbJson --branch main --workers 5
```

同步逻辑：

- 先为每个模块重新生成最新 `raw` 和 `dedup` URL 列表
- 从远端读取 `url/raw`、`url/dedup`、`url/404`
- 只处理新增 URL，以及 `url/404` 里仍需重试的 URL
- 调用现有 `main.py` 管道导出 JSON
- 通过 `gh api` 直接更新：
  - `url/raw/<module>.url.txt`
  - `url/dedup/<module>.url.dedup.txt`
  - `url/404/<module>.txt`
  - `<module>/*.json`

## GitHub Action

- 工作流文件：`.github/workflows/sync-pdb.yml`
- 触发方式：
  - 手动运行
  - 每周自动运行一次
- 不 `checkout` 仓库
- 只用 `gh api` 拉取所需工具文件并上传结果
