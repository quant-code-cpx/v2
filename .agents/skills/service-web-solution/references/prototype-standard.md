# 新页面原型标准

## 交付物

在方案目录内创建：

```text
docs/service-web/<NNNN>-<topic>/
├── index.html
├── prototype.html
└── assets/
    ├── prototype-desktop.png
    └── prototype-states.png      # 远程数据或复杂交互时需要
```

- `prototype.html` 是可编辑源，不是生产页面。
- 默认只使用 `1440×900` PC 桌面视口并生成一份主原型图。
- `index.html` 嵌入或链接桌面图，并说明版本、假设和待决问题。
- 禁止生成移动端、平板、触控设备、窄屏或响应式原型及截图。

## 制作顺序

1. 阅读 canonical tokens、MUI theme、chart tokens、design language 和 page patterns。
2. 用 CodeGraph 搜索可复用 shell、cards、tables、filters、forms、drawers、dialogs 和 chart patterns。
3. 先确定业务问题、信息层级和动作，再绘制高保真静态原型。
4. 使用 repository-native HTML/CSS 或现有前端栈；复用 token 名称和组件几何。
5. 使用 Browser 或 Playwright 渲染一份 `1440×900` 桌面图；不得手工伪造截图。
6. 用视觉检查工具查看生成图片，发现溢出、遮挡、低对比、错误语义后迭代。
7. 记录原型审查结论，再进入生产实现。

## 审查记录

在方案中记录原型版本、桌面视口、审查人或自审、结论、未决项、修改摘要，以及允许进入生产实现的条件。未满足条件时保持原型阶段，不把“已生成截图”视为“已通过审查”。

## 必须表达

- 页面标题、上下文、首要任务和主要动作。
- 首屏关键数据或工作区，不用营销 Hero 占据首屏。
- 导航、筛选、表格/图表/表单等核心结构。
- Loading、empty、error、permission 和成功反馈；可集中到 states 图。
- 数据来源、新鲜度或延迟提示。
- PC 桌面视口内的信息密度、固定区和溢出策略。

## 视觉门禁

- 使用现有白色壳层、4px/8px spacing、既定 radius 和 token colors。
- 中国市场涨跌语义正确，颜色搭配符号或文字。
- 默认桌面视口无水平页面溢出；仅表格或必要图表容器允许内部横向滚动。
- 鼠标 hover、点击、键盘焦点和禁用反馈明确；不设计触控或手势交互。
- 焦点、禁用、加载、错误和成功状态可辨认。
- 不使用 ImageGen、通用 AI dashboard 图、装饰渐变、玻璃、霓虹或随机暗色侧栏。
