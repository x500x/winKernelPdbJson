# PDB 并发导出工具

这个目录现在集中管理单模块 URL 文件的下载、PDB 拉取和 JSON 导出流程。

## 运行方式

```bash
python X:\pdb\main.py --input "X:\winKernelPdbJson\url\dedup\fltmgr.sys.url.dedup.txt" --workers 5 --final-output-root "X:\winKernelPdbJson"
```

## 输入要求

- 一次只处理一个 `*.url.dedup.txt` 或 `*.url.txt`
- 文件中每行一个 URL
- 空行会自动跳过，重复 URL 会自动去重

## 输出结构

- `X:\pdb\runs\<module>\<run_id>\worker_1..worker_5`：并发 worker 工作目录
- `X:\pdb\runs\<module>\<run_id>\merged\exports`：本次运行合并后的 JSON
- `X:\pdb\runs\<module>\<run_id>\merged\logs`：成功、失败、重复和汇总日志
- `X:\winKernelPdbJson\<module>`：最终同步后的正式结果目录

## 常用参数

- `--workers 5`：并发进程数
- `--keep-workdirs`：保留 worker 目录
- `--overwrite`：覆盖正式结果目录中的同名文件
- `--failed-only <failed_urls.txt>`：只重跑上次失败的 URL
