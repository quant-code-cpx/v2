# Web 方案检查表

## 业务理解

- Actor、入口、主业务问题、决策和首要任务明确。
- 次要任务、成功结果、失败恢复和权限边界明确。
- 所需数据、权威来源、新鲜度、敏感性和契约状态明确。
- 新页面/既有页面判断有理由；未知项标为假设或待决。

## 页面与交互

- 信息顺序按决策价值排列，不按后端对象或组件类型排列。
- Route、shell、section、component 和 action 边界明确。
- Loading、empty、stale、partial、error/retry、forbidden、disabled、submitting、success 完整。
- URL、TanStack Query、本地状态和图表引擎状态所有权明确。
- 桌面、平板、移动、键盘、焦点、触控目标和 reduced motion 完整。

## 数据与契约

- 仅调用冻结的 service-api 契约。
- 未冻结能力使用 fixture/MSW，并清楚标注，不猜 endpoint。
- Query key、缓存、失效、刷新、并发请求和错误策略明确。
- 权限、敏感数据隐藏、时间/时区和金融精度语义明确。
- 分享或重访需要的筛选状态进入 URL。

## 视觉与图表

- 使用现有 tokens、theme、组件和页面模式，无随机视觉常量。
- 中国市场红涨绿跌正确，颜色不是唯一信号。
- KLineChart 与 ECharts 职责不混用。
- 表格数字右对齐、移动端可横向滚动；图表控制不遮挡内容。
- 原型和最终方案无巨大 Hero、装饰渐变、霓虹、玻璃或暗色壳层。

## 性能与交付

- 路由懒加载、bundle、payload、缓存、render isolation 和图表销毁明确。
- Skeleton 接近最终几何，关键内容有合理 perceived loading。
- 发布、兼容、feature flag、回滚和埋点/监控策略明确。
- `vp check`、现有测试、`vp build`、相关 E2E、Docker build 和 `/healthz` 命令明确。
