# 架构决策记录

使用 ADR 保存影响架构、数据契约、部署或多个服务的重要决定。

## 命名

文件名格式：

```text
NNNN-short-title.md
```

编号递增且不复用。已废弃的 ADR 保留，并链接替代它的新 ADR。

## 流程

1. 复制 `0000-template.md`。
2. 填写背景、约束、候选方案和取舍。
3. 状态先设为 `Proposed`。
4. 评审后改为 `Accepted`、`Rejected`、`Deprecated` 或 `Superseded`。
5. 实现与文档链接对应 ADR。

